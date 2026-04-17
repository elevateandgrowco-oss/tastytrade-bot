"""
Tastytrade MES Futures Bot
Strategy: Thomas Wade 4-Channel Price Action + TJR ICT Concepts
Instrument: MES (Micro E-mini S&P 500) — $5/point
Auth: OAuth 2.0 — no device challenge, no lockouts

On first deploy: visit the Railway URL and click "Authorize" to log in once.
After that, trades automatically forever.
"""

import os
import sys
import json
import time
import threading
import requests
import yfinance as yf
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode

# ── Config ─────────────────────────────────────────────────────────────────────
TT_BASE_URL     = os.getenv("TT_BASE_URL", "https://api.tastytrade.com")
TT_CLIENT_ID    = os.getenv("TT_CLIENT_ID", "")
TT_CLIENT_SECRET= os.getenv("TT_CLIENT_SECRET", "")
TT_REDIRECT_URI = os.getenv("TT_REDIRECT_URI", "https://tastytrade-bot-production.up.railway.app/callback")
PORT            = int(os.getenv("PORT", "8080"))
TOKENS_FILE     = "oauth_tokens.json"

MAX_TRADES_PER_DAY  = 3
CONTRACTS           = 1
EMA_PERIOD          = 20
RSI_PERIOD          = 3
ATR_PERIOD          = 14
ATR_STOP_MULT       = 1.5
MIN_STOP_POINTS     = 2.0
MAX_STOP_POINTS     = 10.0
EMA_PROXIMITY_PCT   = 1.5
VOLUME_MULT         = 1.2
NEWS_BLOCK_MIN      = 15
AVOID_OPEN_MINUTES  = 15
AVOID_CLOSE_MINUTES = 30
LOG_FILE            = "trades.json"
NEWS_TIMES_ET       = [(8,30),(10,0),(14,0),(14,30)]

# OAuth state
oauth_state = {
    "access_token": None,
    "refresh_token": None,
    "authorized": threading.Event(),
}

# ── Bar object ─────────────────────────────────────────────────────────────────
class Bar:
    def __init__(self, date, open_, high, low, close, volume):
        self.date=date; self.open=open_; self.high=high
        self.low=low; self.close=close; self.volume=volume

# ── Data ───────────────────────────────────────────────────────────────────────
def fetch_bars(interval="5m", period="2d"):
    try:
        df = yf.download("MES=F", period=period, interval=interval,
                         progress=False, auto_adjust=True)
        if df.empty:
            df = yf.download("ES=F", period=period, interval=interval,
                             progress=False, auto_adjust=True)
        if df.empty: return []
        bars = []
        for ts, row in df.iterrows():
            def _v(col):
                v = row[col]
                return float(v.iloc[0] if hasattr(v,"iloc") else v)
            bars.append(Bar(str(ts),_v("Open"),_v("High"),_v("Low"),_v("Close"),int(_v("Volume"))))
        return bars
    except Exception as e:
        print(f"⚠️  yfinance: {e}"); return []

# ── Token persistence ──────────────────────────────────────────────────────────
def save_tokens(access_token, refresh_token):
    with open(TOKENS_FILE, "w") as f:
        json.dump({"access_token": access_token, "refresh_token": refresh_token}, f)
    oauth_state["access_token"] = access_token
    oauth_state["refresh_token"] = refresh_token

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            data = json.load(f)
            oauth_state["access_token"] = data.get("access_token")
            oauth_state["refresh_token"] = data.get("refresh_token")
            return True
    return False

# ── OAuth web server ───────────────────────────────────────────────────────────
class OAuthHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args): pass

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            if code:
                try:
                    self._exchange_code(code)
                    self._respond("<h2>✅ Authorized! Bot is now trading MES futures.</h2><p>You can close this tab.</p>")
                except Exception as e:
                    self._respond(f"<h2>❌ Error: {e}</h2>")
            else:
                self._respond("<h2>❌ No code received from Tastytrade.</h2>")
            return

        # Home page
        if oauth_state["access_token"]:
            html = "<h2>✅ Tastytrade Bot Running</h2><p>Trading MES futures — Thomas Wade strategy active.</p>"
        else:
            auth_url = (
                f"https://api.tastytrade.com/oauth/authorize?"
                + urlencode({
                    "client_id": TT_CLIENT_ID,
                    "redirect_uri": TT_REDIRECT_URI,
                    "response_type": "code",
                    "scope": "read trade",
                })
            )
            html = f"""
<html><body style="font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center">
<h2>Tastytrade Bot — Authorization Required</h2>
<p>Click below to authorize the bot to trade on your behalf.</p>
<a href="{auth_url}" style="background:#e85d04;color:white;padding:15px 30px;text-decoration:none;font-size:18px;border-radius:5px">
  Authorize with Tastytrade
</a>
</body></html>"""
        self._respond(html)

    def _exchange_code(self, code):
        r = requests.post("https://api.tastytrade.com/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": TT_REDIRECT_URI,
            "client_id": TT_CLIENT_ID,
            "client_secret": TT_CLIENT_SECRET,
        })
        if r.status_code not in (200, 201):
            raise Exception(f"Token exchange failed: {r.status_code} {r.text}")
        data = r.json()
        save_tokens(data["access_token"], data.get("refresh_token", ""))
        print("✅ OAuth authorized! Access token received.")
        oauth_state["authorized"].set()

    def _respond(self, body):
        html = f"<html><body style='font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center'>{body}</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), OAuthHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"🌐 Web server on port {PORT}")

# ── Tastytrade API client ───────────────────────────────────────────────────────
def auth_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {oauth_state['access_token']}",
    }

def refresh_access_token():
    r = requests.post("https://api.tastytrade.com/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": oauth_state["refresh_token"],
        "client_id": TT_CLIENT_ID,
        "client_secret": TT_CLIENT_SECRET,
    })
    if r.status_code in (200, 201):
        data = r.json()
        save_tokens(data["access_token"], data.get("refresh_token", oauth_state["refresh_token"]))
        print("✅ Access token refreshed")
        return True
    return False

def get_account_number():
    r = requests.get(f"{TT_BASE_URL}/customers/me/accounts", headers=auth_headers())
    r.raise_for_status()
    accounts = r.json()["data"]["items"]
    for a in accounts:
        if a["account"]["margin-or-cash"] == "Margin":
            return a["account"]["account-number"]
    return accounts[0]["account"]["account-number"]

def get_mes_position(account_number):
    r = requests.get(f"{TT_BASE_URL}/accounts/{account_number}/positions", headers=auth_headers())
    r.raise_for_status()
    for p in r.json()["data"]["items"]:
        if "MES" in p.get("symbol", ""):
            qty = int(p.get("quantity", 0))
            return qty if p.get("quantity-direction","Long") == "Long" else -qty
    return 0

def get_mes_symbol():
    r = requests.get(f"{TT_BASE_URL}/instruments/futures",
                     params={"product-code": "MES"}, headers=auth_headers())
    r.raise_for_status()
    items = r.json()["data"]["items"]
    today = str(datetime.now(timezone.utc).date())
    active = [i for i in items if i.get("expiration-date","9999") >= today]
    if not active: active = items
    active.sort(key=lambda x: x.get("expiration-date","9999"))
    symbol = active[0]["symbol"]
    print(f"  MES: {symbol} (exp {active[0].get('expiration-date')})")
    return symbol

def place_order(account_number, symbol, side, qty, current_pos):
    if current_pos != 0:
        action = "Buy to Close" if side == "Buy" else "Sell to Close"
    else:
        action = "Buy to Open" if side == "Buy" else "Sell to Open"
    payload = {
        "time-in-force": "Day",
        "order-type": "Market",
        "legs": [{"instrument-type": "Future", "symbol": symbol, "quantity": qty, "action": action}]
    }
    r = requests.post(f"{TT_BASE_URL}/accounts/{account_number}/orders",
                      json=payload, headers=auth_headers())
    if r.status_code not in (200, 201):
        raise Exception(f"Order failed: {r.status_code} {r.text}")
    order_id = r.json()["data"].get("order", {}).get("id", "unknown")
    print(f"✅ Order placed — {action} {qty} {symbol} | ID {order_id}")
    return order_id

# ── Indicators ─────────────────────────────────────────────────────────────────
def calc_ema(closes, period):
    if len(closes) < period: return None
    mult = 2/(period+1); ema = sum(closes[:period])/period
    for c in closes[period:]: ema = c*mult + ema*(1-mult)
    return ema

def calc_rsi(closes, period=3):
    if len(closes) < period+1: return None
    gains = losses = 0
    for i in range(len(closes)-period, len(closes)):
        d = closes[i]-closes[i-1]
        if d > 0: gains += d
        else: losses -= d
    ag,al = gains/period, losses/period
    if al == 0: return 100
    return 100 - 100/(1+ag/al)

def calc_atr(bars, period=14):
    if len(bars) < period+1: return None
    trs = [max(bars[i].high-bars[i].low, abs(bars[i].high-bars[i-1].close),
               abs(bars[i].low-bars[i-1].close)) for i in range(1,len(bars))]
    return sum(trs[-period:])/period

def calc_vwap(bars):
    cv=cv2=0
    for b in bars:
        tp=(b.high+b.low+b.close)/3; cv+=tp*b.volume; cv2+=b.volume
    return cv/cv2 if cv2 else None

def avg_volume(bars, period=20):
    vols=[b.volume for b in bars[-period:] if b.volume>0]
    return sum(vols)/len(vols) if vols else 0

def get_15m_bias():
    try:
        bars=fetch_bars("15m","5d")
        if len(bars)<EMA_PERIOD: return None
        closes=[b.close for b in bars]; ema=calc_ema(closes,EMA_PERIOD)
        return "bull" if closes[-1]>ema else "bear"
    except: return None

# ── Time filters ───────────────────────────────────────────────────────────────
def is_market_hours():
    now=datetime.now(timezone.utc); m=now.hour*60+now.minute
    return (13*60+35)<=m<=(19*60+55)

def is_avoid_time():
    now=datetime.now(timezone.utc); m=now.hour*60+now.minute
    return m<(13*60+30)+AVOID_OPEN_MINUTES or m>(20*60)-AVOID_CLOSE_MINUTES

def is_near_news():
    now=datetime.now(timezone.utc)
    et=((now.hour-4)%24)*60+now.minute
    for h,m in NEWS_TIMES_ET:
        if abs(et-(h*60+m))<=NEWS_BLOCK_MIN: return True,f"{h:02d}:{m:02d} ET"
    return False,None

# ── Trade log ──────────────────────────────────────────────────────────────────
def load_log():
    if not os.path.exists(LOG_FILE): return {"trades":[]}
    with open(LOG_FILE) as f: return json.load(f)

def save_log(log):
    with open(LOG_FILE,"w") as f: json.dump(log,f,indent=2)

def count_todays_trades(log):
    today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for t in log["trades"] if t["timestamp"].startswith(today) and t.get("order_placed"))

def get_open_trade(log):
    for t in reversed(log["trades"]):
        if t.get("order_placed") and not t.get("closed"): return t
    return None

# ── Strategy ───────────────────────────────────────────────────────────────────
def on_bar(bars, account_number, mes_symbol):
    now_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}\n  Bar close — {now_str}\n{'='*60}")

    if not is_market_hours(): print("🕐 Outside market hours."); return
    if is_avoid_time(): print("⏳ Open/close buffer."); return
    near_news,label=is_near_news()
    if near_news: print(f"📰 News blackout — {label}"); return
    if len(bars)<ATR_PERIOD+EMA_PERIOD: print(f"⏳ Not enough bars ({len(bars)})"); return

    closes=[b.close for b in bars]; price=closes[-1]
    ema20=calc_ema(closes,EMA_PERIOD); rsi3=calc_rsi(closes,RSI_PERIOD)
    atr=calc_atr(bars,ATR_PERIOD); vwap=calc_vwap(bars); bias_15m=get_15m_bias()
    avg_vol=avg_volume(bars,20); last_vol=bars[-1].volume
    dist_pct=abs((price-ema20)/ema20)*100 if ema20 else 0
    stop_pts=max(MIN_STOP_POINTS,min(MAX_STOP_POINTS,(atr*ATR_STOP_MULT) if atr else 4.0))
    target_pts=stop_pts*2

    print(f"  Price:{price:.2f} EMA20:{ema20:.2f}({dist_pct:.2f}%) RSI3:{rsi3:.1f} ATR:{atr:.2f}" if all([ema20,rsi3,atr]) else f"  Price:{price:.2f}")
    print(f"  VWAP:{vwap:.2f} 15m:{bias_15m or 'n/a'} Vol:{last_vol}(avg{avg_vol:.0f})" if vwap else "")

    current_pos=get_mes_position(account_number)
    if current_pos!=0:
        log=load_log(); open_trade=get_open_trade(log)
        entry=open_trade.get("price",price) if open_trade else price
        sp=open_trade.get("stop_pts",stop_pts) if open_trade else stop_pts
        tp=open_trade.get("target_pts",target_pts) if open_trade else target_pts
        be=open_trade.get("breakeven_triggered",False) if open_trade else False
        direction=1 if current_pos>0 else -1
        pts=(price-entry)*direction; pnl=pts*abs(current_pos)*5
        print(f"\n── Open: MES {'+' if current_pos>0 else ''}{current_pos} @ {entry:.2f} | P&L:${pnl:.2f} ({pts:.2f}pts)")
        if not be and pts>=tp/2:
            print("  🔒 BREAKEVEN");
            if open_trade: open_trade["breakeven_triggered"]=True; save_log(log)
            be=True
        eff_stop=0.0 if be else sp; exit_reason=None
        if current_pos>0:
            if ema20 and price<ema20: exit_reason="Price below EMA(20)"
            elif pts<=-eff_stop: exit_reason=f"Stop hit ({pts:.2f}pts)"
            elif pts>=tp: exit_reason=f"Take profit (+{pts:.2f}pts)"
        else:
            if ema20 and price>ema20: exit_reason="Price above EMA(20)"
            elif pts<=-eff_stop: exit_reason=f"Stop hit ({pts:.2f}pts)"
            elif pts>=tp: exit_reason=f"Take profit (+{pts:.2f}pts)"
        if exit_reason:
            print(f"\n🔴 CLOSING — {exit_reason}")
            try:
                place_order(account_number,mes_symbol,"Sell" if current_pos>0 else "Buy",abs(current_pos),current_pos)
                if open_trade:
                    open_trade.update({"closed":True,"exit_price":price,"exit_reason":exit_reason,
                                       "pnl_usd":pnl,"exit_timestamp":datetime.now(timezone.utc).isoformat()})
                    save_log(log)
            except Exception as e: print(f"❌ Close failed: {e}")
        else: print(f"  ✅ Holding — stop:{'BE' if be else f'-{sp}pts'} target:+{tp}pts")
        return

    log=load_log()
    if count_todays_trades(log)>=MAX_TRADES_PER_DAY: print("🚫 Max trades reached"); return
    if ema20 is None or rsi3 is None: print("❌ Insufficient data"); return
    bullish=price>ema20
    checks=([(f"Price above EMA(20)",price>ema20),(f"Within {EMA_PROXIMITY_PCT}% of EMA",dist_pct<EMA_PROXIMITY_PCT),
              ("RSI(3)<40",rsi3<40),("Above VWAP",vwap is None or price>vwap),
              ("15m bullish",bias_15m is None or bias_15m=="bull"),(f"Vol>={VOLUME_MULT}x",avg_vol==0 or last_vol>=avg_vol*VOLUME_MULT)]
             if bullish else
             [(f"Price below EMA(20)",price<ema20),(f"Within {EMA_PROXIMITY_PCT}% of EMA",dist_pct<EMA_PROXIMITY_PCT),
              ("RSI(3)>60",rsi3>60),("Below VWAP",vwap is None or price<vwap),
              ("15m bearish",bias_15m is None or bias_15m=="bear"),(f"Vol>={VOLUME_MULT}x",avg_vol==0 or last_vol>=avg_vol*VOLUME_MULT)])
    print(f"\n── Entry {'LONG' if bullish else 'SHORT'}:")
    all_pass=True
    for label,passed in checks:
        print(f"  {'✅' if passed else '🚫'} {label}")
        if not passed: all_pass=False
    if not all_pass: print("🚫 BLOCKED"); return
    side="Buy" if bullish else "Sell"
    print(f"✅ ENTRY — {side} {CONTRACTS} MES @ ~{price:.2f}")
    try:
        oid=place_order(account_number,mes_symbol,side,CONTRACTS,0)
        log["trades"].append({"timestamp":datetime.now(timezone.utc).isoformat(),"action":side,
            "symbol":mes_symbol,"qty":CONTRACTS,"price":price,"ema20":round(ema20,2),
            "rsi3":round(rsi3,2),"atr":round(atr,2) if atr else None,"vwap":round(vwap,2) if vwap else None,
            "bias_15m":bias_15m,"stop_pts":round(stop_pts,2),"target_pts":round(target_pts,2),
            "breakeven_triggered":False,"closed":False,"order_id":oid,"order_placed":True})
        save_log(log)
    except Exception as e: print(f"❌ Order failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("="*60)
    print(f"  Tastytrade MES Futures Bot — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Strategy: Thomas Wade 4-Channel + TJR ICT | ATR Stops")
    print(f"  Auth: OAuth 2.0")
    print("="*60)

    start_web_server()

    # Try loading saved tokens first
    if load_tokens() and oauth_state["access_token"]:
        print("✅ Loaded saved OAuth tokens")
        oauth_state["authorized"].set()
    else:
        print(f"\n🔐 Visit your Railway URL to authorize:")
        print(f"   https://tastytrade-bot-production.up.railway.app")
        print("   Waiting for authorization...\n")
        oauth_state["authorized"].wait()

    # Get account info
    try:
        account_number = get_account_number()
        print(f"✅ Account: {account_number}")
    except Exception as e:
        print(f"❌ Could not load account: {e}")
        sys.exit(1)

    try:
        mes_symbol = get_mes_symbol()
    except Exception as e:
        print(f"❌ Could not resolve MES: {e}"); sys.exit(1)

    print(f"\n📡 Polling every 60s for 5m bar closes...\n")
    last_bar_time = None
    token_refresh = time.time()

    while True:
        try:
            # Refresh token every 20 minutes
            if time.time() - token_refresh > 1200:
                refresh_access_token()
                token_refresh = time.time()

            bars=fetch_bars("5m","2d")
            if bars and len(bars)>=2:
                completed=bars[:-1]; latest=completed[-1]; bar_time=latest.date
                if bar_time!=last_bar_time:
                    last_bar_time=bar_time; on_bar(completed,account_number,mes_symbol)
                else:
                    if is_market_hours():
                        print(f"⏳ {datetime.now().strftime('%H:%M:%S')} — waiting for next bar")
                    else:
                        print(f"🕐 {datetime.now().strftime('%H:%M:%S')} — outside market hours")
            else:
                print("⚠️  No bar data — market may be closed")
        except Exception as e:
            print(f"❌ Error: {e}")
            if "401" in str(e):
                refresh_access_token()
        time.sleep(60)

if __name__=="__main__":
    main()
