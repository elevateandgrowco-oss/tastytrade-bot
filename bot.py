"""
Tastytrade MES Futures Bot
Strategy: Thomas Wade 4-Channel Price Action + TJR ICT Concepts
Instrument: MES (Micro E-mini S&P 500) — $5/point
Timeframe: 5m entries, 15m bias filter

On first startup, visit the Railway URL to enter your device challenge code.
After that, it trades automatically forever.
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
from urllib.parse import urlparse, parse_qs

# ── Config ─────────────────────────────────────────────────────────────────────
TT_BASE_URL     = os.getenv("TT_BASE_URL", "https://api.tastytrade.com")
TT_USERNAME     = os.getenv("TT_USERNAME", "")
TT_PASSWORD     = os.getenv("TT_PASSWORD", "")
PORT            = int(os.getenv("PORT", "8080"))

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

NEWS_TIMES_ET = [(8,30),(10,0),(14,0),(14,30)]

# Global state for device challenge flow
challenge_state = {
    "token": None,         # X-Tastyworks-Challenge-Token from Tastytrade
    "code": None,          # SMS code entered by user
    "waiting": False,      # True when waiting for user to enter code
    "done": threading.Event(),
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
        if df.empty:
            return []
        bars = []
        for ts, row in df.iterrows():
            def _v(col):
                v = row[col]
                return float(v.iloc[0] if hasattr(v,"iloc") else v)
            bars.append(Bar(str(ts),_v("Open"),_v("High"),_v("Low"),_v("Close"),int(_v("Volume"))))
        return bars
    except Exception as e:
        print(f"⚠️  yfinance: {e}")
        return []

# ── Web server for device challenge ────────────────────────────────────────────
class ChallengeHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress request logs

    def do_GET(self):
        if challenge_state["waiting"]:
            html = """
<html><body style="font-family:sans-serif;max-width:400px;margin:80px auto;text-align:center">
<h2>Tastytrade Device Verification</h2>
<p>Enter the code sent to your phone:</p>
<form method="POST" action="/challenge">
  <input name="code" style="font-size:24px;width:150px;text-align:center" autofocus>
  <br><br>
  <button type="submit" style="font-size:18px;padding:10px 30px">Submit</button>
</form>
</body></html>"""
        else:
            html = """
<html><body style="font-family:sans-serif;max-width:400px;margin:80px auto;text-align:center">
<h2>✅ Tastytrade Bot Running</h2>
<p>Trading MES futures — Thomas Wade strategy active.</p>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type","text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        params = parse_qs(body)
        code = params.get("code", [""])[0].strip()
        if code:
            challenge_state["code"] = code
            challenge_state["done"].set()
        self.send_response(200)
        self.send_header("Content-Type","text/html")
        self.end_headers()
        self.wfile.write(b"<html><body style='font-family:sans-serif;max-width:400px;margin:80px auto;text-align:center'><h2>Code submitted! Bot is authorizing...</h2></body></html>")

def start_web_server():
    server = HTTPServer(("0.0.0.0", PORT), ChallengeHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    print(f"🌐 Web server running on port {PORT}")

# ── Tastytrade client ───────────────────────────────────────────────────────────
class TastytradeClient:
    def __init__(self):
        self.session_token = None
        self.account_number = None
        self.headers = {"Content-Type": "application/json"}

    def login(self):
        remember_token = os.getenv("TT_REMEMBER_TOKEN", "")
        if remember_token:
            payload = {"login": TT_USERNAME, "remember-token": remember_token, "remember-me": True}
        else:
            payload = {"login": TT_USERNAME, "password": TT_PASSWORD, "remember-me": True}

        r = requests.post(f"{TT_BASE_URL}/sessions", json=payload, headers=self.headers)

        if r.status_code in (200, 201):
            data = r.json()["data"]
            self.session_token = data["session-token"]
            self.headers["Authorization"] = self.session_token
            print("✅ Logged in to Tastytrade")
            self._load_account()
            return

        if r.status_code == 403:
            err = r.json().get("error", {})
            if err.get("code") == "device_challenge_required":
                self._handle_device_challenge(r)
                return

        raise Exception(f"Login failed: {r.status_code} {r.text}")

    def _handle_device_challenge(self, r403):
        challenge_token = r403.headers.get("X-Tastyworks-Challenge-Token", "")
        challenge_state["token"] = challenge_token
        challenge_state["waiting"] = True
        challenge_state["done"].clear()

        print("\n" + "="*55)
        print("  DEVICE VERIFICATION REQUIRED")
        print("="*55)
        print(f"  Visit your Railway URL to enter the SMS code")
        print(f"  Tastytrade is sending a code to your phone...")
        print("="*55 + "\n")

        # Wait for user to enter code via web UI (up to 10 minutes)
        challenge_state["done"].wait(timeout=600)

        code = challenge_state.get("code", "")
        if not code:
            raise Exception("Device challenge timed out — no code entered within 10 minutes")

        challenge_state["waiting"] = False
        challenge_headers = {
            "Content-Type": "application/json",
            "X-Tastyworks-Challenge-Token": challenge_token,
        }

        # Submit the device challenge code
        r2 = requests.post(f"{TT_BASE_URL}/device-challenge", json={"code": code},
                           headers=challenge_headers)
        if r2.status_code not in (200, 201):
            raise Exception(f"Device challenge failed: {r2.status_code} {r2.text}")
        print("✅ Device challenge accepted!")

        # Check if 2FA OTP is also required
        r2_data = r2.json().get("data", {})
        redirect_headers = r2_data.get("redirect", {}).get("required-headers", [])

        if "X-Tastyworks-OTP" in str(redirect_headers):
            # Need OTP — get it via web
            challenge_state["code"] = None
            challenge_state["waiting"] = True
            challenge_state["done"].clear()
            print("📱 2FA code required — waiting for second code via web UI...")
            challenge_state["done"].wait(timeout=300)
            otp = challenge_state.get("code", "")
            challenge_state["waiting"] = False
            challenge_headers["X-Tastyworks-OTP"] = otp

        # Final login with challenge token (+ OTP if needed)
        r3 = requests.post(f"{TT_BASE_URL}/sessions", json={
            "login": TT_USERNAME,
            "password": TT_PASSWORD,
            "remember-me": True,
        }, headers=challenge_headers)

        if r3.status_code not in (200, 201):
            raise Exception(f"Final login failed: {r3.status_code} {r3.text}")

        data = r3.json()["data"]
        remember = data.get("remember-token", "")
        print(f"\n✅ Authorized! Remember token for Railway:\n  TT_REMEMBER_TOKEN={remember}\n")

        self.session_token = data["session-token"]
        self.headers["Authorization"] = self.session_token
        print("✅ Logged in to Tastytrade")
        self._load_account()

    def _load_account(self):
        r = requests.get(f"{TT_BASE_URL}/customers/me/accounts", headers=self.headers)
        r.raise_for_status()
        accounts = r.json()["data"]["items"]
        for a in accounts:
            if a["account"]["margin-or-cash"] == "Margin":
                self.account_number = a["account"]["account-number"]
                break
        if not self.account_number:
            self.account_number = accounts[0]["account"]["account-number"]
        print(f"✅ Account: {self.account_number}")

    def get_mes_position(self):
        r = requests.get(f"{TT_BASE_URL}/accounts/{self.account_number}/positions",
                         headers=self.headers)
        r.raise_for_status()
        for p in r.json()["data"]["items"]:
            if "MES" in p.get("symbol", ""):
                qty = int(p.get("quantity", 0))
                return qty if p.get("quantity-direction","Long") == "Long" else -qty
        return 0

    def get_mes_symbol(self):
        r = requests.get(f"{TT_BASE_URL}/instruments/futures",
                         params={"product-code": "MES"}, headers=self.headers)
        r.raise_for_status()
        items = r.json()["data"]["items"]
        today = datetime.now(timezone.utc).date()
        active = [i for i in items if i.get("expiration-date","9999") >= str(today)]
        if not active:
            active = items
        active.sort(key=lambda x: x.get("expiration-date","9999"))
        symbol = active[0]["symbol"]
        print(f"  MES contract: {symbol} (expires {active[0].get('expiration-date')})")
        return symbol

    def place_order(self, symbol, side, qty):
        current_pos = self.get_mes_position()
        if current_pos != 0:
            action = "Buy to Close" if side == "Buy" else "Sell to Close"
        else:
            action = "Buy to Open" if side == "Buy" else "Sell to Open"
        payload = {
            "time-in-force": "Day",
            "order-type": "Market",
            "legs": [{"instrument-type": "Future", "symbol": symbol,
                      "quantity": qty, "action": action}]
        }
        r = requests.post(f"{TT_BASE_URL}/accounts/{self.account_number}/orders",
                          json=payload, headers=self.headers)
        if r.status_code not in (200, 201):
            raise Exception(f"Order failed: {r.status_code} {r.text}")
        order_id = r.json()["data"].get("order", {}).get("id", "unknown")
        print(f"✅ Order placed — ID {order_id} | {action} {qty} {symbol}")
        return order_id

    def keep_session_alive(self):
        try:
            r = requests.put(f"{TT_BASE_URL}/sessions", headers=self.headers)
            if r.status_code == 200:
                token = r.json()["data"]["session-token"]
                self.session_token = token
                self.headers["Authorization"] = token
        except Exception as e:
            print(f"⚠️  Session refresh failed: {e}")

# ── Indicators ─────────────────────────────────────────────────────────────────
def calc_ema(closes, period):
    if len(closes) < period: return None
    mult = 2/(period+1)
    ema = sum(closes[:period])/period
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
        closes=[b.close for b in bars]
        ema=calc_ema(closes,EMA_PERIOD)
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
def on_bar(bars, tt, mes_symbol):
    now_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*60}\n  Bar close — {now_str}\n{'='*60}")

    if not is_market_hours(): print("🕐 Outside market hours."); return
    if is_avoid_time(): print("⏳ Open/close buffer."); return
    near_news,label=is_near_news()
    if near_news: print(f"📰 News blackout — {label}"); return
    if len(bars)<ATR_PERIOD+EMA_PERIOD: print(f"⏳ Not enough bars ({len(bars)})"); return

    closes=[b.close for b in bars]
    price=closes[-1]; ema20=calc_ema(closes,EMA_PERIOD); rsi3=calc_rsi(closes,RSI_PERIOD)
    atr=calc_atr(bars,ATR_PERIOD); vwap=calc_vwap(bars); bias_15m=get_15m_bias()
    avg_vol=avg_volume(bars,20); last_vol=bars[-1].volume
    dist_pct=abs((price-ema20)/ema20)*100 if ema20 else 0
    raw_stop=(atr*ATR_STOP_MULT) if atr else 4.0
    stop_pts=max(MIN_STOP_POINTS,min(MAX_STOP_POINTS,raw_stop))
    target_pts=stop_pts*2

    print(f"  Price:{price:.2f} EMA20:{ema20:.2f}({dist_pct:.2f}%) RSI3:{rsi3:.1f}" if ema20 and rsi3 else f"  Price:{price:.2f}")
    print(f"  ATR:{atr:.2f} Stop:{stop_pts:.1f}pts Target:{target_pts:.1f}pts" if atr else "  ATR:n/a")
    print(f"  VWAP:{vwap:.2f} 15m:{bias_15m or 'n/a'} Vol:{last_vol}(avg{avg_vol:.0f})" if vwap else f"  15m:{bias_15m or 'n/a'}")

    # Check open position
    current_pos=tt.get_mes_position()
    if current_pos!=0:
        direction=1 if current_pos>0 else -1
        log=load_log(); open_trade=get_open_trade(log)
        entry=open_trade.get("price",price) if open_trade else price
        sp=open_trade.get("stop_pts",stop_pts) if open_trade else stop_pts
        tp=open_trade.get("target_pts",target_pts) if open_trade else target_pts
        be=open_trade.get("breakeven_triggered",False) if open_trade else False
        pts=(price-entry)*direction
        pnl=pts*abs(current_pos)*5

        print(f"\n── Open: MES {'+' if current_pos>0 else ''}{current_pos} @ {entry:.2f} | P&L:${pnl:.2f} ({pts:.2f}pts)")

        if not be and pts>=tp/2:
            print("  🔒 BREAKEVEN — stop moved to entry")
            if open_trade: open_trade["breakeven_triggered"]=True; save_log(log)
            be=True

        eff_stop=0.0 if be else sp
        exit_reason=None
        if current_pos>0:
            if ema20 and price<ema20: exit_reason="Price below EMA(20)"
            elif pts<=-eff_stop: exit_reason=f"{'Breakeven' if be else 'Stop'} hit ({pts:.2f}pts)"
            elif pts>=tp: exit_reason=f"Take profit (+{pts:.2f}pts)"
        else:
            if ema20 and price>ema20: exit_reason="Price above EMA(20)"
            elif pts<=-eff_stop: exit_reason=f"{'Breakeven' if be else 'Stop'} hit ({pts:.2f}pts)"
            elif pts>=tp: exit_reason=f"Take profit (+{pts:.2f}pts)"

        if exit_reason:
            close_side="Sell" if current_pos>0 else "Buy"
            print(f"\n🔴 CLOSING — {exit_reason}")
            try:
                tt.place_order(mes_symbol,close_side,abs(current_pos))
                if open_trade:
                    open_trade.update({"closed":True,"exit_price":price,"exit_reason":exit_reason,
                                       "pnl_usd":pnl,"exit_timestamp":datetime.now(timezone.utc).isoformat()})
                    save_log(log)
            except Exception as e: print(f"❌ Close failed: {e}")
        else:
            print(f"  ✅ Holding — stop:{'entry(BE)' if be else f'-{sp}pts'} target:+{tp}pts")
        return

    # Entry check
    log=load_log()
    if count_todays_trades(log)>=MAX_TRADES_PER_DAY:
        print(f"🚫 Max trades reached"); return
    if ema20 is None or rsi3 is None: print("❌ Insufficient data"); return

    bullish=price>ema20
    checks = (
        [(f"Price above EMA(20)",price>ema20),(f"Within {EMA_PROXIMITY_PCT}% of EMA",dist_pct<EMA_PROXIMITY_PCT),
         ("RSI(3)<40 — pullback",rsi3<40),("Price above VWAP",vwap is None or price>vwap),
         ("15m bullish",bias_15m is None or bias_15m=="bull"),(f"Vol>={VOLUME_MULT}x avg",avg_vol==0 or last_vol>=avg_vol*VOLUME_MULT)]
        if bullish else
        [(f"Price below EMA(20)",price<ema20),(f"Within {EMA_PROXIMITY_PCT}% of EMA",dist_pct<EMA_PROXIMITY_PCT),
         ("RSI(3)>60 — pullback",rsi3>60),("Price below VWAP",vwap is None or price<vwap),
         ("15m bearish",bias_15m is None or bias_15m=="bear"),(f"Vol>={VOLUME_MULT}x avg",avg_vol==0 or last_vol>=avg_vol*VOLUME_MULT)]
    )
    print(f"\n── Entry {'LONG' if bullish else 'SHORT'} checks:")
    all_pass=True
    for label,passed in checks:
        print(f"  {'✅' if passed else '🚫'} {label}")
        if not passed: all_pass=False

    if not all_pass: print("🚫 BLOCKED"); return

    side="Buy" if bullish else "Sell"
    print(f"✅ ENTRY — {side} {CONTRACTS} MES @ ~{price:.2f}")
    try:
        oid=tt.place_order(mes_symbol,side,CONTRACTS)
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
    print("="*60)

    start_web_server()

    if not TT_USERNAME or not TT_PASSWORD:
        print("❌ TT_USERNAME and TT_PASSWORD must be set"); sys.exit(1)

    tt = TastytradeClient()
    while True:
        try:
            tt.login()
            break
        except Exception as e:
            print(f"❌ Login failed: {e}")
            print("⏳ Waiting 5 minutes before retrying...")
            time.sleep(300)

    try:
        mes_symbol = tt.get_mes_symbol()
    except Exception as e:
        print(f"❌ Could not resolve MES symbol: {e}"); sys.exit(1)

    print(f"\n📡 Polling every 60s for 5m bar closes...\n")
    last_bar_time = None
    session_refresh = 0

    while True:
        try:
            if time.time()-session_refresh > 1800:
                tt.keep_session_alive(); session_refresh=time.time()

            bars=fetch_bars("5m","2d")
            if bars and len(bars)>=2:
                completed=bars[:-1]; latest=completed[-1]; bar_time=latest.date
                if bar_time!=last_bar_time:
                    last_bar_time=bar_time; on_bar(completed,tt,mes_symbol)
                else:
                    if is_market_hours():
                        print(f"⏳ {datetime.now().strftime('%H:%M:%S')} — waiting for next bar")
                    else:
                        print(f"🕐 {datetime.now().strftime('%H:%M:%S')} — outside market hours")
            else:
                print("⚠️  No bar data — market may be closed")
        except Exception as e:
            print(f"❌ Error: {e}")
            if "401" in str(e) or "session" in str(e).lower():
                print("⏳ Session expired — waiting 5 min before re-login...")
                time.sleep(300)
                try: tt.login()
                except Exception as le: print(f"❌ Re-login failed: {le}")
        time.sleep(60)

if __name__=="__main__":
    main()
