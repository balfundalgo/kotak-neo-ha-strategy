"""
Live 1-minute Heikin Ashi candles - Kotak Neo
============================================
Resolves ATM CE/PE for NIFTY, SENSEX, CRUDEOILM (plus their spot),
streams ticks, and builds 1-minute Heikin Ashi candles.

This is the CANDLE LAYER only - no entries/exits yet. Verify the candles
against your charting platform before we add strategy rules on top.

Run:
    python ha_live.py                # all instruments
    python ha_live.py --spot-only    # indices/futures only (lighter)
"""

import time
import argparse
import threading
from datetime import datetime, timezone, timedelta

from kotak_ws_base import login, _ALIAS, _lock
from option_chain import (
    UNDERLYINGS, load_scrip_master, option_rows,
    near_month_future, normalise_strikes, MAX_TOTAL_SCRIPS,
)
from candle_engine import EnginePool, WARMUP_CANDLES, IST

POOL = EnginePool()
_names = {}            # display bookkeeping
_tick_count = {"n": 0}


# ---------------------------------------------------------------------------
# Tick -> candle
# ---------------------------------------------------------------------------
def on_message(msg):
    """Feed every LTP into its candle engine."""
    if isinstance(msg, dict):
        data = msg.get("data")
        if not isinstance(data, list):
            return
        ticks = data
    elif isinstance(msg, list):
        ticks = msg
    else:
        return

    for tick in ticks:
        tk, ts = tick.get("tk"), tick.get("ts")
        with _lock:
            if tk and ts:
                _ALIAS[str(tk)] = ts
            name = ts or _ALIAS.get(str(tk), tk)
        if not name:
            continue

        price = tick.get("ltp") or tick.get("iv")
        if price is None:
            continue

        # exchange timestamp kept for verification only; bucketing uses
        # local IST arrival because ltt goes stale on illiquid strikes
        exch = tick.get("ltt") or tick.get("fdtm") or tick.get("tvalue")
        POOL.on_tick(name, price, exch_time=exch)
        _tick_count["n"] += 1


def on_candle(name, c):
    arrow = "^" if c["ha_colour"] == "green" else "v"
    warm = "" if POOL.engine(name).is_warm(WARMUP_CANDLES) else "  [warming]"
    print(
        f"{c['bucket']:%H:%M}  {name:<24} "
        f"O {c['open']:>10,.2f}  H {c['high']:>10,.2f}  "
        f"L {c['low']:>10,.2f}  C {c['close']:>10,.2f}  | "
        f"HA {c['ha_open']:>10,.2f} {c['ha_high']:>10,.2f} "
        f"{c['ha_low']:>10,.2f} {c['ha_close']:>10,.2f} {arrow}"
        f"  t={c['ticks']:<4}{warm}"
    )


# ---------------------------------------------------------------------------
# Instrument resolution (same rules as option_chain.py)
# ---------------------------------------------------------------------------
def resolve(client, spot_only=False):
    masters = {}
    for seg in sorted({u["fo_segment"] for u in UNDERLYINGS.values()}):
        masters[seg] = load_scrip_master(client, seg)

    idx_subs, scrip_subs, spot_names = [], [], {}

    for name, cfg in UNDERLYINGS.items():
        if cfg["spot_type"] == "index":
            idx_subs.append({"instrument_token": cfg["spot_name"],
                             "exchange_segment": cfg["spot_segment"]})
            spot_names[name] = cfg["spot_name"]
        else:
            fut = near_month_future(masters[cfg["fo_segment"]],
                                   cfg["fo_segment"], name)
            if not fut:
                print(f"[WARN] {name}: no futures found")
                continue
            with _lock:
                _ALIAS[fut["instrument_token"]] = fut["trd"]
            scrip_subs.append({"instrument_token": fut["instrument_token"],
                               "exchange_segment": fut["exchange_segment"]})
            spot_names[name] = fut["trd"]
            print(f"[FUT ] {name}: {fut['trd']} exp {fut['expiry']}")

    return masters, idx_subs, scrip_subs, spot_names


def resolve_atm(masters, spot_names, spots):
    today = datetime.now(IST).date()
    subs = []
    for name, cfg in UNDERLYINGS.items():
        seg = cfg["fo_segment"]
        spot = spots.get(spot_names.get(name))
        if spot is None:
            print(f"[WARN] {name}: no spot, skipping options")
            continue

        opt, cols = option_rows(masters[seg], seg, name)
        if opt.empty:
            continue
        opt = normalise_strikes(opt, spot)

        exps = sorted(e for e in opt["_expiry"].unique() if e >= today)
        if not exps:
            continue
        chain = opt[opt["_expiry"] == exps[0]]
        strikes = sorted(chain["_strike"].unique())
        atm = min(strikes, key=lambda s: abs(s - spot))

        flag = "  <<< EXPIRY TODAY" if exps[0] == today else ""
        print(f"[ATM ] {name}: {spot:,.2f} -> {atm:,.0f} | exp {exps[0]}{flag}")

        for side in ("CE", "PE"):
            row = chain[(chain["_strike"] == atm) & (chain["_opt"] == side)]
            if row.empty:
                continue
            row = row.iloc[0]
            token, trd = str(row[cols["token"]]), str(row[cols["trd"]])
            with _lock:
                _ALIAS[token] = trd
            print(f"       {side}: {trd:<28} lot {row[cols['lot']]}")
            subs.append({"instrument_token": token, "exchange_segment": seg})
    return subs


def wait_for_prices(names, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = POOL.snapshot()
        got = {n: (snap.get(n) or {}).get("ltp") for n in names}
        if all(v is not None for v in got.values()):
            return got
        time.sleep(0.5)
    snap = POOL.snapshot()
    return {n: (snap.get(n) or {}).get("ltp") for n in names}


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spot-only", action="store_true",
                    help="skip options, stream indices/futures only")
    args = ap.parse_args()

    client = login()
    client.on_message = on_message      # route ticks into the candle engines

    POOL.on_candle_close(on_candle)

    masters, idx_subs, scrip_subs, spot_names = resolve(client, args.spot_only)

    if idx_subs:
        client.subscribe(instrument_tokens=idx_subs, isIndex=True)
    if scrip_subs:
        client.subscribe(instrument_tokens=scrip_subs)

    print("\n[SPOT] waiting for prices...")
    spots = wait_for_prices(list(spot_names.values()))
    for k, v in spot_names.items():
        print(f"[SPOT] {k:<10} {v:<24} {spots.get(v)}")

    opt_subs = []
    if not args.spot_only:
        print()
        opt_subs = resolve_atm(masters, spot_names, spots)
        total = len(idx_subs) + len(scrip_subs) + len(opt_subs)
        if total > MAX_TOTAL_SCRIPS:
            raise ValueError(f"{total} subscriptions exceeds {MAX_TOTAL_SCRIPS}")
        if opt_subs:
            client.subscribe(instrument_tokens=opt_subs)

    POOL.start_roller(interval=1.0)

    print(f"\n[RUN ] building 1-min Heikin Ashi candles "
          f"(first {WARMUP_CANDLES} marked [warming])")
    print("[RUN ] Ctrl-C to stop\n")

    try:
        while True:
            time.sleep(30)
            snap = POOL.snapshot()
            warm = sum(1 for s in snap.values() if s["warm"])
            print(f"    -- {datetime.now(IST):%H:%M:%S} "
                  f"{len(snap)} instruments | {warm} warm | "
                  f"{_tick_count['n']:,} ticks --")
    except KeyboardInterrupt:
        print("\n[RUN ] stopping")
        POOL.stop()
        try:
            if opt_subs:
                client.un_subscribe(instrument_tokens=opt_subs)
            if scrip_subs:
                client.un_subscribe(instrument_tokens=scrip_subs)
            if idx_subs:
                client.un_subscribe(instrument_tokens=idx_subs, isIndex=True)
            client.logout()
        except Exception as e:
            print(f"[RUN ] cleanup error: {e}")


if __name__ == "__main__":
    main()
