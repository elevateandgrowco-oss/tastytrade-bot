"""
Tastytrade MES Futures Bot
Strategy: Thomas Wade 4-Channel Price Action + TJR ICT Concepts
Instrument: MES (Micro E-mini S&P 500) — $5/point
Timeframe: 5m entries, 15m bias filter
Entry: Second entry pullback to EMA(20) + RSI(3) + VWAP + volume
Exit: ATR-based stop/target + EMA cross exit + breakeven at 1:1

Runs on Railway — no desktop app needed.
Pure REST API via Tastytrade's official API.
"""

import os
import sys
import json
import time
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────────────
TT_BASE_URL     = os.getenv("TT_BASE_URL", "https://api.tastytrade.com")
TT_USERNAME     = os.getenv("TT_USERNAME", "")
TT_PASSWORD     = os.getenv("TT_PASSWORD", "")

MAX_TRADES_PER_DAY  = 3
CONTRACTS           = 1
EMA_PERIOD          = 20
RSI_PERIOD          = 3
ATR_PERIOD          = 14
ATR_STOP_MULT       = 1.5
ATR_TARGET_MULT     = 3.0
MIN_STOP_POINTS     = 2.0
MAX_STOP_POINTS     = 10.0
EMA_PROXIMITY_PCT   = 1.5
VOLUME_MULT         = 1.2
NEWS_BLOCK_MIN      = 15
AVOID_OPEN_MINUTES  = 15
AVOID_CLOSE_MINUTES = 30
LOG_FILE            = "trades.json"

NEWS_TIMES_ET = [
    (8, 30),
    (10, 0),
    (14, 0),
    (14, 30),
]

# ── Lightweight bar object ──────────────────────────────────────────────────────
class Bar:
    def __init__(self, date, open_, high, low, close, volume):
        self.date   = date
        self.open   = open_
        self.high   = high
        self.low    = low
        self.close  = close
        self.volume = volume

# ── Data fetch ─────────────────────────────────────────────────────────────────
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
            def _val(col):
                v = row[col]
                return float(v.iloc[0] if hasattr(v, "iloc") else v)
            bars.append(Bar(
                date   = str(ts),
                open_  = _val("Open"),
                high   = _val("High"),
                low    = _val("Low"),
                close  = _val("Close"),
                volume = int(_val("Volume")),
            ))
        return bars
    except Exception as e:
        print(f"⚠️  yfinance error: {e}")
        return []

# ── Tastytrade API session ──────────────────────────────────────────────────────
class TastytradeClient:
    def __init__(self):
        self.session_token = None
        self.account_number = None
        self.headers = {"Content-Type": "application/json"}

    def login(self):
        url = f"{TT_BASE_URL}/sessions"
        payload = {"login": TT_USERNAME, "password": TT_PASSWORD}
        r = requests.post(url, json=payload, headers=self.headers)
        if r.status_code not in (200, 201):
            raise Exception(f"Login failed: {r.status_code} {r.text}")
        data = r.json()["data"]
        self.session_token = data["session-token"]
        self.headers["Authorization"] = self.session_token
        print(f"✅ Logged in to Tastytrade")
        self._load_account()

    def _load_account(self):
        r = requests.get(f"{TT_BASE_URL}/customers/me/accounts", headers=self.headers)
        r.raise_for_status()
        accounts = r.json()["data"]["items"]
        # Prefer margin account; fall back to first
        for a in accounts:
            if a["account"]["margin-or-cash"] == "Margin":
                self.account_number = a["account"]["account-number"]
                break
        if not self.account_number:
            self.account_number = accounts[0]["account"]["account-number"]
        print(f"✅ Account: {self.account_number}")

    def get_positions(self):
        r = requests.get(
            f"{TT_BASE_URL}/accounts/{self.account_number}/positions",
            headers=self.headers
        )
        r.raise_for_status()
        return r.json()["data"]["items"]

    def get_mes_position(self):
        """Return current MES position qty (positive=long, negative=short, 0=flat)."""
        positions = self.get_positions()
        for p in positions:
            if "MES" in p.get("symbol", ""):
                qty = int(p.get("quantity", 0))
                direction = p.get("quantity-direction", "Long")
                return qty if direction == "Long" else -qty
        return 0

    def get_mes_symbol(self):
        """Get the front-month MES futures symbol (e.g. /MESM5)."""
        r = requests.get(
            f"{TT_BASE_URL}/instruments/futures",
            params={"product-code": "MES"},
            headers=self.headers
        )
        r.raise_for_status()
        items = r.json()["data"]["items"]
        # Filter to active (not expired) contracts, pick nearest expiration
        today = datetime.now(timezone.utc).date()
        active = [i for i in items if i.get("active-month", False) or
                  (i.get("expiration-date", "9999") >= str(today))]
        if not active:
            active = items
        active.sort(key=lambda x: x.get("expiration-date", "9999"))
        symbol = active[0]["symbol"]
        print(f"  MES contract: {symbol} (expires {active[0].get('expiration-date')})")
        return symbol

    def place_order(self, symbol, side, qty):
        """
        side: 'Buy' or 'Sell'
        Places a market order for MES futures.
        """
        order_action = "Buy to Open" if side == "Buy" else "Sell to Open"
        # Check if we're closing an existing position
        current_pos = self.get_mes_position()
        if current_pos != 0:
            order_action = "Buy to Close" if side == "Buy" else "Sell to Close"

        payload = {
            "time-in-force": "Day",
            "order-type": "Market",
            "legs": [
                {
                    "instrument-type": "Future",
                    "symbol": symbol,
                    "quantity": qty,
                    "action": order_action,
                }
            ]
        }
        r = requests.post(
            f"{TT_BASE_URL}/accounts/{self.account_number}/orders",
            json=payload,
            headers=self.headers
        )
        if r.status_code not in (200, 201):
            raise Exception(f"Order failed: {r.status_code} {r.text}")
        data = r.json()["data"]
        order_id = data.get("order", {}).get("id", "unknown")
        print(f"✅ Order placed — ID {order_id} | {order_action} {qty} {symbol}")
        return order_id

    def keep_session_alive(self):
        """Refresh session — call periodically to prevent timeout."""
        try:
            r = requests.put(
                f"{TT_BASE_URL}/sessions",
                headers=self.headers
            )
            if r.status_code == 200:
                new_token = r.json()["data"]["session-token"]
                self.session_token = new_token
                self.headers["Authorization"] = new_token
        except Exception as e:
            print(f"⚠️  Session refresh failed: {e}")

# ── Indicators ─────────────────────────────────────────────────────────────────
def calc_ema(closes, period):
    if len(closes) < period:
        return None
    mult = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * mult + ema * (1 - mult)
    return ema

def calc_rsi(closes, period=3):
    if len(closes) < period + 1:
        return None
    gains = losses = 0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100
    return 100 - 100 / (1 + avg_gain / avg_loss)

def calc_atr(bars, period=14):
    if len(bars) < period + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period

def calc_vwap(bars):
    cum_tp_vol = cum_vol = 0
    for b in bars:
        typical = (b.high + b.low + b.close) / 3
        cum_tp_vol += typical * b.volume
        cum_vol += b.volume
    return cum_tp_vol / cum_vol if cum_vol > 0 else None

def avg_volume(bars, period=20):
    vols = [b.volume for b in bars[-period:] if b.volume > 0]
    return sum(vols) / len(vols) if vols else 0

def get_15m_bias():
    try:
        bars_15m = fetch_bars(interval="15m", period="5d")
        if len(bars_15m) < EMA_PERIOD:
            return None
        closes = [b.close for b in bars_15m]
        ema = calc_ema(closes, EMA_PERIOD)
        return "bull" if closes[-1] > ema else "bear"
    except Exception:
        return None

# ── Time filters ───────────────────────────────────────────────────────────────
def is_market_hours():
    now = datetime.now(timezone.utc)
    utc_min = now.hour * 60 + now.minute
    return (13 * 60 + 35) <= utc_min <= (19 * 60 + 55)

def is_avoid_time():
    now = datetime.now(timezone.utc)
    utc_min = now.hour * 60 + now.minute
    too_early = utc_min < (13 * 60 + 30) + AVOID_OPEN_MINUTES
    too_late  = utc_min > (20 * 60 + 0) - AVOID_CLOSE_MINUTES
    return too_early or too_late

def is_near_news():
    now_utc = datetime.now(timezone.utc)
    et_offset = 4  # EDT (Mar–Nov); change to 5 in winter
    et_hour = (now_utc.hour - et_offset) % 24
    et_min  = now_utc.minute
    now_et_minutes = et_hour * 60 + et_min
    for (h, m) in NEWS_TIMES_ET:
        if abs(now_et_minutes - (h * 60 + m)) <= NEWS_BLOCK_MIN:
            return True, f"{h:02d}:{m:02d} ET"
    return False, None

# ── Trade log ──────────────────────────────────────────────────────────────────
def load_log():
    if not os.path.exists(LOG_FILE):
        return {"trades": []}
    with open(LOG_FILE) as f:
        return json.load(f)

def save_log(log):
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

def count_todays_trades(log):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for t in log["trades"]
               if t["timestamp"].startswith(today) and t.get("order_placed"))

def get_open_trade(log):
    for t in reversed(log["trades"]):
        if t.get("order_placed") and not t.get("closed"):
            return t
    return None

# ── Strategy ───────────────────────────────────────────────────────────────────
def on_bar(bars, tt: TastytradeClient, mes_symbol: str):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 60}")
    print(f"  Bar close — {now_str}")
    print(f"{'=' * 60}")

    if not is_market_hours():
        print("🕐 Outside market hours — waiting.")
        return

    if is_avoid_time():
        print(f"⏳ Avoid time (open/close buffer)")
        return

    near_news, news_label = is_near_news()
    if near_news:
        print(f"📰 News blackout — within {NEWS_BLOCK_MIN} min of {news_label}")
        return

    if len(bars) < ATR_PERIOD + EMA_PERIOD:
        print(f"⏳ Not enough bars yet ({len(bars)})")
        return

    closes   = [b.close for b in bars]
    price    = closes[-1]
    ema20    = calc_ema(closes, EMA_PERIOD)
    rsi3     = calc_rsi(closes, RSI_PERIOD)
    atr      = calc_atr(bars, ATR_PERIOD)
    vwap     = calc_vwap(bars)
    bias_15m = get_15m_bias()
    avg_vol  = avg_volume(bars, 20)
    last_vol = bars[-1].volume
    dist_pct = abs((price - ema20) / ema20) * 100 if ema20 else 0

    raw_stop   = (atr * ATR_STOP_MULT) if atr else 4.0
    stop_pts   = max(MIN_STOP_POINTS, min(MAX_STOP_POINTS, raw_stop))
    target_pts = stop_pts * 2

    print(f"  Price:    {price:.2f}")
    print(f"  EMA(20):  {ema20:.2f}  ({dist_pct:.2f}% away)" if ema20 else "  EMA(20):  n/a")
    print(f"  RSI(3):   {rsi3:.1f}" if rsi3 is not None else "  RSI(3):   n/a")
    print(f"  ATR(14):  {atr:.2f}  → stop {stop_pts:.1f} pts / target {target_pts:.1f} pts" if atr else "  ATR:      n/a")
    print(f"  VWAP:     {vwap:.2f}" if vwap else "  VWAP:     n/a")
    print(f"  15m bias: {bias_15m or 'n/a'}")
    print(f"  Volume:   {last_vol} (avg {avg_vol:.0f})")

    # ── Check open position ────────────────────────────────────────────────────
    current_pos = tt.get_mes_position()

    if current_pos != 0:
        direction = 1 if current_pos > 0 else -1
        log = load_log()
        open_trade = get_open_trade(log)

        entry_price          = open_trade.get("price", price) if open_trade else price
        stop_pts_trade       = open_trade.get("stop_pts", stop_pts) if open_trade else stop_pts
        target_pts_trade     = open_trade.get("target_pts", target_pts) if open_trade else target_pts
        breakeven_triggered  = open_trade.get("breakeven_triggered", False) if open_trade else False

        points_from_entry = (price - entry_price) * direction
        unrealized_pnl    = points_from_entry * abs(current_pos) * 5

        print(f"\n── Open Position ──────────────────────────────────────────")
        print(f"  MES {'+' if current_pos > 0 else ''}{current_pos} @ {entry_price:.2f}")
        print(f"  P&L: ${unrealized_pnl:.2f} ({points_from_entry:.2f} pts)")

        # Move stop to breakeven at 1:1
        if not breakeven_triggered and points_from_entry >= target_pts_trade / 2:
            print(f"  🔒 BREAKEVEN triggered — stop moved to entry")
            if open_trade:
                open_trade["breakeven_triggered"] = True
                save_log(log)
            breakeven_triggered = True

        effective_stop = 0.0 if breakeven_triggered else stop_pts_trade
        exit_reason = None

        if current_pos > 0:  # Long
            if ema20 and price < ema20:
                exit_reason = "Price crossed below EMA(20)"
            elif points_from_entry <= -effective_stop:
                exit_reason = f"{'Breakeven' if breakeven_triggered else 'Stop'} hit ({points_from_entry:.2f} pts)"
            elif points_from_entry >= target_pts_trade:
                exit_reason = f"Take profit hit (+{points_from_entry:.2f} pts)"
        else:  # Short
            if ema20 and price > ema20:
                exit_reason = "Price crossed above EMA(20)"
            elif points_from_entry <= -effective_stop:
                exit_reason = f"{'Breakeven' if breakeven_triggered else 'Stop'} hit ({points_from_entry:.2f} pts)"
            elif points_from_entry >= target_pts_trade:
                exit_reason = f"Take profit hit (+{points_from_entry:.2f} pts)"

        if exit_reason:
            close_side = "Sell" if current_pos > 0 else "Buy"
            print(f"\n🔴 CLOSING — {exit_reason}")
            try:
                tt.place_order(mes_symbol, close_side, abs(current_pos))
                if open_trade:
                    open_trade.update({
                        "closed": True,
                        "exit_price": price,
                        "exit_reason": exit_reason,
                        "pnl_usd": unrealized_pnl,
                        "exit_timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    save_log(log)
            except Exception as e:
                print(f"❌ Close order failed: {e}")
        else:
            print(f"  ✅ Holding — stop at {'entry (breakeven)' if breakeven_triggered else f'-{stop_pts_trade} pts'} | target +{target_pts_trade} pts")
        return

    # ── No open position — check entry ─────────────────────────────────────────
    log = load_log()
    today_trades = count_todays_trades(log)

    print(f"\n── Trade Limits ───────────────────────────────────────────")
    if today_trades >= MAX_TRADES_PER_DAY:
        print(f"🚫 Max trades reached: {today_trades}/{MAX_TRADES_PER_DAY}")
        return
    print(f"  ✅ Trades today: {today_trades}/{MAX_TRADES_PER_DAY}")

    if ema20 is None or rsi3 is None:
        print("❌ Not enough data for indicators")
        return

    bullish = price > ema20

    print(f"\n── Entry Checks ({'LONG' if bullish else 'SHORT'}) ─────────────────────────────────")

    if bullish:
        checks = [
            ("Price above EMA(20) — uptrend confirmed",                 price > ema20),
            (f"Within {EMA_PROXIMITY_PCT}% of EMA — second entry zone", dist_pct < EMA_PROXIMITY_PCT),
            ("RSI(3) below 40 — pullback mature",                       rsi3 < 40),
            ("Price above VWAP — institutional flow bullish",           vwap is None or price > vwap),
            ("15m trend bullish — higher timeframe confirms",           bias_15m is None or bias_15m == "bull"),
            (f"Volume >= {VOLUME_MULT}x avg — conviction bar",          avg_vol == 0 or last_vol >= avg_vol * VOLUME_MULT),
        ]
    else:
        checks = [
            ("Price below EMA(20) — downtrend confirmed",               price < ema20),
            (f"Within {EMA_PROXIMITY_PCT}% of EMA — second entry zone", dist_pct < EMA_PROXIMITY_PCT),
            ("RSI(3) above 60 — pullback mature",                       rsi3 > 60),
            ("Price below VWAP — institutional flow bearish",           vwap is None or price < vwap),
            ("15m trend bearish — higher timeframe confirms",           bias_15m is None or bias_15m == "bear"),
            (f"Volume >= {VOLUME_MULT}x avg — conviction bar",          avg_vol == 0 or last_vol >= avg_vol * VOLUME_MULT),
        ]

    all_pass = True
    for label, passed in checks:
        print(f"  {'✅' if passed else '🚫'} {label}")
        if not passed:
            all_pass = False

    print(f"\n── Decision ───────────────────────────────────────────────")
    if not all_pass:
        print("🚫 TRADE BLOCKED — conditions not met")
        return

    side = "Buy" if bullish else "Sell"
    print(f"✅ ALL CONDITIONS MET — placing {side} {CONTRACTS} MES")
    try:
        order_id = tt.place_order(mes_symbol, side, CONTRACTS)
        print(f"   Entry: ~{price:.2f}")
        print(f"   Stop:  {stop_pts:.1f} pts (${stop_pts * 5:.0f}) | Target: {target_pts:.1f} pts (${target_pts * 5:.0f})")
        log["trades"].append({
            "timestamp":          datetime.now(timezone.utc).isoformat(),
            "action":             side,
            "symbol":             mes_symbol,
            "qty":                CONTRACTS,
            "price":              price,
            "ema20":              round(ema20, 2),
            "rsi3":               round(rsi3, 2),
            "atr":                round(atr, 2) if atr else None,
            "vwap":               round(vwap, 2) if vwap else None,
            "bias_15m":           bias_15m,
            "stop_pts":           round(stop_pts, 2),
            "target_pts":         round(target_pts, 2),
            "breakeven_triggered": False,
            "closed":             False,
            "order_id":           order_id,
            "order_placed":       True,
        })
        save_log(log)
    except Exception as e:
        print(f"❌ Order failed: {e}")


# ── Main loop ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(f"  Tastytrade MES Futures Bot — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Strategy: Thomas Wade 4-Channel + TJR ICT | ATR Stops")
    print(f"  Instrument: MES (Micro E-mini S&P 500) — $5/point")
    print(f"  API: {TT_BASE_URL}")
    print("=" * 60)

    if not TT_USERNAME or not TT_PASSWORD:
        print("❌ TT_USERNAME and TT_PASSWORD must be set in environment")
        sys.exit(1)

    tt = TastytradeClient()
    try:
        tt.login()
    except Exception as e:
        print(f"❌ Login failed: {e}")
        sys.exit(1)

    try:
        mes_symbol = tt.get_mes_symbol()
    except Exception as e:
        print(f"❌ Could not resolve MES symbol: {e}")
        sys.exit(1)

    print(f"\n📡 Polling yfinance every 60s for 5m bar closes...\n")

    last_bar_time    = None
    session_refresh  = 0

    while True:
        try:
            # Refresh session every 30 minutes
            if time.time() - session_refresh > 1800:
                tt.keep_session_alive()
                session_refresh = time.time()

            bars = fetch_bars(interval="5m", period="2d")

            if bars and len(bars) >= 2:
                completed       = bars[:-1]
                latest_closed   = completed[-1]
                bar_time        = latest_closed.date

                if bar_time != last_bar_time:
                    last_bar_time = bar_time
                    on_bar(completed, tt, mes_symbol)
                else:
                    now_str = datetime.now().strftime("%H:%M:%S")
                    if is_market_hours():
                        print(f"⏳ {now_str} — waiting for next 5m bar (last: {bar_time})")
                    else:
                        print(f"🕐 {now_str} — outside market hours")
            else:
                print(f"⚠️  No bar data — market may be closed")

        except Exception as e:
            print(f"❌ Error: {e}")
            # Try to re-login on auth errors
            if "401" in str(e) or "session" in str(e).lower():
                try:
                    print("🔄 Re-logging in...")
                    tt.login()
                except Exception as le:
                    print(f"❌ Re-login failed: {le}")

        time.sleep(60)


if __name__ == "__main__":
    main()
