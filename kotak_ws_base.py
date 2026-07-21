"""
Kotak Neo API v2 - WebSocket base client
=========================================
Login (TOTP + MPIN) -> connect -> subscribe -> stream live LTPs.

Usage:
    1. Fill in credentials and instruments in config.py
    2. python kotak_ws_base.py

Ctrl-C to stop cleanly (unsubscribes and logs out).
"""

import time
import threading
from datetime import datetime

import pyotp
from neo_api_client import NeoAPI

from config_loader import CONFIG, INDICES, SCRIPS

# ---------------------------------------------------------------------------
# Official websocket limits (Kotak Neo docs)
# ---------------------------------------------------------------------------
MAX_TOTAL_SCRIPS = 200      # total concurrent subscriptions allowed
MAX_PER_REQUEST = 100       # per-request cap enforced by the feed library
MAX_CHANNELS = 16           # total channels available

# ---------------------------------------------------------------------------
# Field mapping (official). NOTE the bid/ask asymmetry:
#   bp = Best Bid Price   bq = Best Bid Qty
#   sp = Best Ask Price   bs = Best Ask QTY  <-- ask side, not bid size
#
#   tk  Exchange Token        ts  Trading Symbol      e    Exchange
#   ltp Last Traded Price     ltq Last Traded Qty     ap   Avg Traded Price
#   tbq Total Buy Qty         tsq Total Sell Qty      to   Turnover
#   op  Open    h High    lo Low    c Previous Close
#   cng Change  nc % Change   oi Open Interest
#   ltt Last Trade Time       fdtm Feed Time          prec Price Precision
#   lcl Lower Circuit         ucl Upper Circuit
#   yh  52wk High             yl  52wk Low            mul  Price Multiplier
#   name Feed Type: 'sf' = scrip, 'if' = index (index LTP arrives as 'iv')
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Live LTP cache
# ---------------------------------------------------------------------------
LTP = {}                    # name -> merged tick dict (incl. "ltp")
_ALIAS = {}                 # numeric token -> trading symbol (e.g. "11536" -> "TCS-EQ")
_LAST = {}                  # name -> last printed price (dedupe duplicate streams)
_lock = threading.Lock()


def get_ltp(name):
    """Thread-safe read of the latest LTP for a symbol/index name."""
    with _lock:
        row = LTP.get(name)
        return row.get("ltp") if row else None


def snapshot():
    """Thread-safe copy of the whole LTP cache."""
    with _lock:
        return {k: v.copy() for k, v in LTP.items()}


# ---------------------------------------------------------------------------
# WebSocket callbacks
# ---------------------------------------------------------------------------
def on_open(msg):
    print(f"[WS ] open    : {msg}")


def on_close(msg):
    print(f"[WS ] close   : {msg}")


def on_error(err):
    print(f"[WS ] ERROR   : {err}")


def _extract_ticks(msg):
    """
    The SDK delivers ticks as {'type': 'stock_feed', 'data': [ {...}, {...} ]}.
    Occasionally a bare list. Anything else is a status/ack message.
    """
    if isinstance(msg, dict):
        data = msg.get("data")
        return data if isinstance(data, list) else []
    if isinstance(msg, list):
        return msg
    return []


def on_message(msg):
    """
    Field notes from the live feed:
      - scrips  -> 'ltp'; SNAP carries 'ts' (TCS-EQ), UPDATEs carry only 'tk' (11536)
      - indices -> 'iv';  name always in 'tk' (Nifty 50)
      - updates are PARTIAL: only changed fields are sent, so we merge.

    We learn tk -> ts from the SNAP so every tick for one instrument lands
    under a single stable key instead of splitting across token and symbol.
    """
    ticks = _extract_ticks(msg)

    if not ticks:
        if isinstance(msg, dict) and "data" not in msg:
            print(f"[ACK] {msg}")
        return

    for tick in ticks:
        tk = tick.get("tk")
        ts = tick.get("ts")

        with _lock:
            if tk and ts:
                _ALIAS[str(tk)] = ts          # learn mapping from SNAP
            name = ts or _ALIAS.get(str(tk), tk)

        if not name:
            continue

        price = tick.get("ltp") or tick.get("iv")

        with _lock:
            row = LTP.setdefault(name, {})
            row.update(tick)                  # merge partial update
            if price is not None:
                try:
                    row["ltp"] = float(price)
                except (TypeError, ValueError):
                    price = None
            row["ts_update"] = datetime.now()

            # suppress identical repeats from the duplicated stream
            if price is not None:
                if _LAST.get(name) == row.get("ltp"):
                    continue
                _LAST[name] = row.get("ltp")

        if price is not None:
            kind = "IDX" if tick.get("name") == "if" else "SCR"
            print(f"{datetime.now():%H:%M:%S} [{kind}] "
                  f"{name:<24} {row['ltp']:>12,.2f}")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def login():
    """TOTP login + MPIN validate. Returns an authenticated NeoAPI client."""
    client = NeoAPI(
        environment=CONFIG.get("environment", "prod"),
        access_token=None,
        neo_fin_key=None,
        consumer_key=CONFIG["consumer_key"],
    )

    client.on_message = on_message
    client.on_error = on_error
    client.on_close = on_close
    client.on_open = on_open

    otp = pyotp.TOTP(CONFIG["totp_secret"]).now()
    print(f"[AUTH] generated TOTP: {otp}")

    resp1 = client.totp_login(
        mobile_number=CONFIG["mobile_number"],
        ucc=CONFIG["ucc"],
        totp=otp,
    )
    print(f"[AUTH] totp_login    : {resp1}")

    resp2 = client.totp_validate(mpin=CONFIG["mpin"])
    print(f"[AUTH] totp_validate : {resp2}")

    return client


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    client = login()

    total = len(INDICES) + len(SCRIPS)
    if total > MAX_TOTAL_SCRIPS:
        raise ValueError(
            f"{total} subscriptions requested but the broker allows a maximum "
            f"of {MAX_TOTAL_SCRIPS} at a time. Trim INDICES/SCRIPS in config.py."
        )
    print(f"[SUB] {total}/{MAX_TOTAL_SCRIPS} subscriptions")

    if INDICES:
        for i in range(0, len(INDICES), MAX_PER_REQUEST):
            batch = INDICES[i:i + MAX_PER_REQUEST]
            print(f"[SUB] {len(batch)} index/indices")
            client.subscribe(instrument_tokens=batch, isIndex=True)
            time.sleep(1)

    if SCRIPS:
        for i in range(0, len(SCRIPS), MAX_PER_REQUEST):
            batch = SCRIPS[i:i + MAX_PER_REQUEST]
            print(f"[SUB] {len(batch)} scrip(s)")
            client.subscribe(instrument_tokens=batch)
            time.sleep(1)

    print("\n[RUN] streaming... Ctrl-C to stop\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[RUN] shutting down")
        try:
            if INDICES:
                client.un_subscribe(instrument_tokens=INDICES, isIndex=True)
            if SCRIPS:
                client.un_subscribe(instrument_tokens=SCRIPS)
            client.logout()
        except Exception as e:
            print(f"[RUN] cleanup error: {e}")
        print("[RUN] done")


if __name__ == "__main__":
    main()
