"""
Delta Exchange India signal bot (runs on GitHub Actions)
--------------------------------------------------------
Signals, evaluated ONLY on closed candles of your chosen timeframe(s):
  1) 200 EMA touch  - candle range (low..high) touches the 200 EMA
  2) EMA cross      - fast EMA crosses slow EMA, confirmed at candle close

Alerts are sent to Telegram (works on phone + desktop).
State (last processed candle per timeframe) is stored in state.json so a
delayed or skipped GitHub run never causes missed or duplicate alerts.

Usage:
  python signal_bot.py                 # normal run
  python signal_bot.py --find-symbol   # list gold-related products on Delta India

Not financial advice. Test before trusting.
"""
import json
import os
import sys
import time

import pandas as pd
import requests

BASE = "https://api.india.delta.exchange"
RES_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800,
}
STATE_FILE = "state.json"


def env(name, default):
    return os.getenv(name) or default   # treats empty strings as "not set"


SYMBOL = env("SYMBOL", "XAUTUSD")                      # Delta India gold symbol
TIMEFRAMES = [t.strip() for t in env("TIMEFRAMES", "15m").split(",")]
EMA_TREND = int(env("EMA_TREND", "200"))
EMA_FAST = int(env("EMA_FAST", "9"))
EMA_SLOW = int(env("EMA_SLOW", "15"))
CANDLES = 600                                          # history (needs > EMA_TREND)


def find_symbols():
    r = requests.get(f"{BASE}/v2/products", timeout=20)
    r.raise_for_status()
    for p in r.json().get("result", []):
        blob = json.dumps(p).upper()
        if any(k in blob for k in ("XAU", "PAXG", "GOLD")):
            print(p.get("symbol"), "-", p.get("description"))


def fetch_candles(symbol, resolution):
    sec = RES_SECONDS[resolution]
    end = int(time.time())
    start = end - CANDLES * sec
    r = requests.get(
        f"{BASE}/v2/history/candles",
        params={"symbol": symbol, "resolution": resolution, "start": start, "end": end},
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json().get("result", [])
    if not rows:
        raise RuntimeError(f"No candles for {symbol} {resolution} - check SYMBOL")
    df = pd.DataFrame(rows).sort_values("time").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    # keep only CLOSED candles (candle 'time' is its start time)
    df = df[df["time"] + sec <= time.time()].reset_index(drop=True)
    return df


def send(text):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat, "text": text},
        timeout=20,
    )
    r.raise_for_status()   # if this fails, state is not saved -> retried next run


def ist(ts):
    return pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata").strftime("%d %b %H:%M IST")


def check_timeframe(tf, state):
    df = fetch_candles(SYMBOL, tf)
    if len(df) < EMA_TREND + 5:
        print(f"[{tf}] not enough candles")
        return
    df["ema_t"] = df["close"].ewm(span=EMA_TREND, adjust=False).mean()
    df["ema_f"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_s"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["touch"] = (df["low"] <= df["ema_t"]) & (df["high"] >= df["ema_t"])

    last_done = state.get(tf)
    if last_done is None:                       # first run: only the latest closed candle
        idxs = [len(df) - 1]
    else:                                       # catch up on anything since last run
        idxs = list(df.index[df["time"] > last_done])

    for i in idxs:
        if i < 1:
            continue
        row, prev = df.iloc[i], df.iloc[i - 1]
        when, px = ist(row["time"]), row["close"]

        # 1) 200 EMA touch (only on the first candle of a touch, not every candle)
        if row["touch"] and not prev["touch"]:
            side = "from ABOVE (possible support)" if prev["close"] > prev["ema_t"] else "from BELOW (possible resistance)"
            send(f"⚪ {SYMBOL} {tf}: price touched {EMA_TREND} EMA {side}\n"
                 f"EMA{EMA_TREND}: {row['ema_t']:.2f}  |  Close: {px:.2f}\n{when}")

        # 2) EMA cross confirmed on candle close
        up = prev["ema_f"] <= prev["ema_s"] and row["ema_f"] > row["ema_s"]
        dn = prev["ema_f"] >= prev["ema_s"] and row["ema_f"] < row["ema_s"]
        if up or dn:
            trend = "above" if px > row["ema_t"] else "below"
            send(f"{'🟢' if up else '🔴'} {SYMBOL} {tf}: EMA{EMA_FAST}/{EMA_SLOW} "
                 f"cross {'UP (BUY bias)' if up else 'DOWN (SELL bias)'} on candle close\n"
                 f"Close: {px:.2f}  ({trend} EMA{EMA_TREND})\n{when}")

    if len(df):
        state[tf] = int(df["time"].iloc[-1])


def main():
    if "--find-symbol" in sys.argv:
        find_symbols()
        return
    for tf in TIMEFRAMES:
        if tf not in RES_SECONDS:
            sys.exit(f"Unsupported timeframe '{tf}'. Use one of: {', '.join(RES_SECONDS)}")
    state = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    for tf in TIMEFRAMES:
        check_timeframe(tf, state)
        json.dump(state, open(STATE_FILE, "w"))   # save after each timeframe succeeds


if __name__ == "__main__":
    main()
