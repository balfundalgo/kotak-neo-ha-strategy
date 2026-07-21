"""
Paper strategy - first-candle HA reference, stop-and-reverse
============================================================

Rules
-----
1. Start before 09:00 (MCX opens first).
2. Before the bell, subscribe a BAND of strikes around an estimated ATM
   (estimated from previous close) so first-minute candles exist for
   whichever strike turns out to be ATM.
3. At session open + 1 minute:
       - lock ATM from the spot/futures first-candle close
       - reference = HA CLOSE of that option's first candle
       - discard the rest of the band
   Session open: 09:15 for nse_fo / bse_fo, 09:00 for mcx_fo.
4. From the next candle onward, on every closed candle:
       HA close > reference  -> target LONG
       HA close < reference  -> target SHORT
       HA close = reference  -> hold
   Stop-and-reverse: if target != current state, close and open opposite in
   one action. First entry is 1 lot; every reversal is 2 lots
   (1 to close + 1 to open). No cap on trades.
5. CE and PE are fully independent - both can be long, both short, or opposite.

Signals use the Heikin Ashi close. Fills use the RAW candle close, since HA
is a synthetic price you cannot trade at (see FILL_PRICE in strategy_config).

PAPER TRADING ONLY - places no orders.
"""

import time as _time
import threading
from datetime import datetime, timedelta

import pandas as pd

from kotak_ws_base import login, _ALIAS, _lock
from option_chain import (
    UNDERLYINGS, load_scrip_master, option_rows,
    near_month_future, MAX_TOTAL_SCRIPS,
)
from candle_engine import EnginePool, IST
from strategy_config import (
    SESSION_OPEN, BAND, UNSUBSCRIBE_BAND_AFTER_LOCK,
    LOCK_DELAY_SEC, FILL_PRICE, STATUS_EVERY,
)

POOL = EnginePool()
PREV_CLOSE = {}          # name -> previous close from the feed
LEGS = {}                # symbol -> Leg (only the 6 locked legs)
_state_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Position / paper book
# ---------------------------------------------------------------------------
class Leg:
    def __init__(self, symbol, token, segment, underlying, side, strike, lot):
        self.symbol = symbol
        self.token = token
        self.segment = segment
        self.underlying = underlying
        self.side = side                # CE / PE
        self.strike = strike
        self.lot = int(lot)

        self.reference = None
        self.state = "FLAT"             # FLAT / LONG / SHORT
        self.entry_price = None
        self.entry_time = None
        self.realized = 0.0
        self.trades = []
        self.last_price = None

    # -- paper fills ----------------------------------------------------
    def _open(self, side, px, ts):
        self.state = side
        self.entry_price = px
        self.entry_time = ts

    def _close(self, px, ts):
        if self.state == "LONG":
            pnl = (px - self.entry_price) * self.lot
        elif self.state == "SHORT":
            pnl = (self.entry_price - px) * self.lot
        else:
            return 0.0
        self.realized += pnl
        self.trades.append({
            "side": self.state, "entry": self.entry_price, "exit": px,
            "in": self.entry_time, "out": ts, "pnl": pnl,
        })
        return pnl

    def unrealized(self, px=None):
        px = px if px is not None else self.last_price
        if px is None or self.state == "FLAT":
            return 0.0
        if self.state == "LONG":
            return (px - self.entry_price) * self.lot
        return (self.entry_price - px) * self.lot

    # -- signal ---------------------------------------------------------
    def evaluate(self, candle):
        if self.reference is None:
            return

        ha = candle["ha_close"]
        fill = candle["ha_close"] if FILL_PRICE == "ha_close" else candle["close"]
        ts = candle["bucket"]
        self.last_price = candle["close"]

        if ha > self.reference:
            target = "LONG"
        elif ha < self.reference:
            target = "SHORT"
        else:
            print(f"{ts:%H:%M}  {self.symbol:<26} HA {ha:>9,.2f} == ref "
                  f"{self.reference:>9,.2f}  hold ({self.state})")
            return

        if target == self.state:
            return

        prev = self.state
        pnl = self._close(fill, ts) if prev != "FLAT" else 0.0
        self._open(target, fill, ts)

        lots = 1 if prev == "FLAT" else 2
        qty = self.lot * lots
        act = "BUY" if target == "LONG" else "SELL"
        tag = "ENTRY" if prev == "FLAT" else f"REVERSE {prev}->{target}"
        pnl_s = f" | closed P&L {pnl:>+10,.2f}" if prev != "FLAT" else ""

        print(f"{ts:%H:%M}  {self.symbol:<26} HA {ha:>9,.2f} vs ref "
              f"{self.reference:>9,.2f}  {act} {qty:>4} ({lots} lot"
              f"{'s' if lots > 1 else ''}) @ {fill:>9,.2f}  [{tag}]{pnl_s}")


# ---------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------
def on_message(msg):
    if isinstance(msg, dict):
        data = msg.get("data")
        ticks = data if isinstance(data, list) else []
    elif isinstance(msg, list):
        ticks = msg
    else:
        return

    for tick in ticks:
        tk, ts_sym = tick.get("tk"), tick.get("ts")
        with _lock:
            if tk and ts_sym:
                _ALIAS[str(tk)] = ts_sym
            name = ts_sym or _ALIAS.get(str(tk), tk)
        if not name:
            continue

        # previous close: 'c' for scrips, 'ic' for indices
        pc = tick.get("c") or tick.get("ic")
        if pc is not None and name not in PREV_CLOSE:
            try:
                PREV_CLOSE[name] = float(pc)
            except (TypeError, ValueError):
                pass

        price = tick.get("ltp") or tick.get("iv")
        if price is None:
            continue
        POOL.on_tick(name, price)


def on_candle(name, candle):
    with _state_lock:
        leg = LEGS.get(name)
    if leg:
        leg.evaluate(candle)


# ---------------------------------------------------------------------------
# Band resolution
# ---------------------------------------------------------------------------
def build_band(masters, underlying, cfg, estimate):
    """Return band leg rows around an estimated ATM."""
    seg = cfg["fo_segment"]
    opt, cols = option_rows(masters[seg], seg, underlying)
    if opt.empty:
        print(f"[WARN] {underlying}: no options in {seg}")
        return [], None

    today = datetime.now(IST).date()
    exps = sorted(e for e in opt["_expiry"].unique() if e >= today)
    if not exps:
        print(f"[WARN] {underlying}: no unexpired contracts")
        return [], None
    expiry = exps[0]

    chain = opt[opt["_expiry"] == expiry]
    strikes = sorted(chain["_strike"].unique())
    centre = min(strikes, key=lambda s: abs(s - estimate))
    i = strikes.index(centre)
    band = strikes[max(0, i - BAND): i + BAND + 1]

    print(f"[BAND] {underlying}: est {estimate:,.2f} -> centre {centre:,.0f} "
          f"| exp {expiry} | {len(band)} strikes "
          f"[{band[0]:,.0f} .. {band[-1]:,.0f}]")

    rows = []
    for strike in band:
        for side in ("CE", "PE"):
            r = chain[(chain["_strike"] == strike) & (chain["_opt"] == side)]
            if r.empty:
                continue
            r = r.iloc[0]
            token, trd = str(r[cols["token"]]), str(r[cols["trd"]])
            with _lock:
                _ALIAS[token] = trd
            rows.append({
                "symbol": trd, "token": token, "segment": seg,
                "underlying": underlying, "side": side,
                "strike": float(strike),
                "lot": int(r[cols["lot"]]) if cols["lot"] else 1,
            })
    return rows, expiry


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def first_candle(engine, ref_bucket):
    """
    Candle stamped exactly at the session-open minute. Falls back to the
    earliest available candle (with a warning) if that one is missing.
    """
    hist = engine.history(600)
    for c in hist:
        if c["bucket"] == ref_bucket:
            return c, False
    for c in hist:
        if c["bucket"] > ref_bucket:
            return c, True
    return None, False


def lock_underlying(underlying, cfg, band_rows, spot_name, ref_bucket):
    """Lock ATM and set references. Returns (kept_symbols, dropped_subs)."""
    spot_engine = POOL.engines.get(spot_name)
    if spot_engine is None:
        print(f"[LOCK] {underlying}: no spot engine, skipping")
        return [], []

    spot_c, late = first_candle(spot_engine, ref_bucket)
    if spot_c is None:
        print(f"[LOCK] {underlying}: no spot candle yet, skipping")
        return [], []
    if late:
        print(f"[LOCK] {underlying}: spot reference candle missing at "
              f"{ref_bucket:%H:%M}, using {spot_c['bucket']:%H:%M}")

    spot_px = spot_c["close"]
    strikes = sorted({r["strike"] for r in band_rows})
    if not strikes:
        return [], []
    atm = min(strikes, key=lambda s: abs(s - spot_px))

    print(f"\n[LOCK] {underlying}: spot first candle close {spot_px:,.2f} "
          f"-> ATM {atm:,.0f}")

    kept, dropped = [], []
    for r in band_rows:
        if r["strike"] != atm:
            dropped.append({"instrument_token": r["token"],
                            "exchange_segment": r["segment"]})
            continue

        eng = POOL.engines.get(r["symbol"])
        if eng is None:
            print(f"       {r['side']}: {r['symbol']} - no candles, skipped")
            continue

        c, late = first_candle(eng, ref_bucket)
        if c is None:
            print(f"       {r['side']}: {r['symbol']} - no candle, skipped")
            continue
        if late:
            print(f"       [WARN] {r['symbol']}: no ticks at "
                  f"{ref_bucket:%H:%M}, reference taken from "
                  f"{c['bucket']:%H:%M}")

        leg = Leg(r["symbol"], r["token"], r["segment"], underlying,
                  r["side"], r["strike"], r["lot"])
        leg.reference = c["ha_close"]
        with _state_lock:
            LEGS[r["symbol"]] = leg
        kept.append(r["symbol"])

        print(f"       {r['side']}: {r['symbol']:<26} ref HA close "
              f"{leg.reference:>9,.2f}  (O {c['open']:,.2f} H {c['high']:,.2f} "
              f"L {c['low']:,.2f} C {c['close']:,.2f})  lot {leg.lot}")

    return kept, dropped


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def print_status():
    with _state_lock:
        legs = list(LEGS.values())
    if not legs:
        return
    print(f"\n{'=' * 108}")
    print(f"{datetime.now(IST):%H:%M:%S}  PAPER BOOK")
    print(f"{'instrument':<26}{'ref':>10}{'last':>10}{'state':>7}"
          f"{'entry':>10}{'qty':>7}{'realized':>12}{'unreal':>12}{'trades':>8}")
    print("-" * 108)
    tot_r = tot_u = 0.0
    for lg in sorted(legs, key=lambda x: (x.underlying, x.side)):
        u = lg.unrealized()
        tot_r += lg.realized
        tot_u += u
        print(f"{lg.symbol:<26}{lg.reference or 0:>10,.2f}"
              f"{lg.last_price or 0:>10,.2f}{lg.state:>7}"
              f"{lg.entry_price or 0:>10,.2f}{lg.lot if lg.state != 'FLAT' else 0:>7}"
              f"{lg.realized:>+12,.2f}{u:>+12,.2f}{len(lg.trades):>8}")
    print("-" * 108)
    print(f"{'TOTAL':<70}{tot_r:>+12,.2f}{tot_u:>+12,.2f}"
          f"{'  net ' + format(tot_r + tot_u, '+,.2f'):>20}")
    print(f"{'=' * 108}\n")


# ---------------------------------------------------------------------------
def main():
    client = login()
    client.on_message = on_message
    POOL.on_candle_close(on_candle)

    masters = {}
    for seg in sorted({u["fo_segment"] for u in UNDERLYINGS.values()}):
        masters[seg] = load_scrip_master(client, seg)

    # ---- spot subscriptions -------------------------------------------
    idx_subs, fut_subs, spot_names = [], [], {}
    for name, cfg in UNDERLYINGS.items():
        if cfg["spot_type"] == "index":
            idx_subs.append({"instrument_token": cfg["spot_name"],
                             "exchange_segment": cfg["spot_segment"]})
            spot_names[name] = cfg["spot_name"]
        else:
            fut = near_month_future(masters[cfg["fo_segment"]],
                                   cfg["fo_segment"], name)
            if not fut:
                print(f"[WARN] {name}: no futures")
                continue
            with _lock:
                _ALIAS[fut["instrument_token"]] = fut["trd"]
            fut_subs.append({"instrument_token": fut["instrument_token"],
                             "exchange_segment": fut["exchange_segment"]})
            spot_names[name] = fut["trd"]
            print(f"[FUT ] {name}: {fut['trd']} exp {fut['expiry']}")

    if idx_subs:
        client.subscribe(instrument_tokens=idx_subs, isIndex=True)
    if fut_subs:
        client.subscribe(instrument_tokens=fut_subs)

    print("\n[PREV] waiting for previous-close values...")
    deadline = _time.time() + 30
    while _time.time() < deadline:
        if all(spot_names[u] in PREV_CLOSE or spot_names[u] in POOL.engines
               for u in spot_names):
            break
        _time.sleep(0.5)

    # ---- band subscriptions -------------------------------------------
    print()
    bands, band_subs = {}, []
    for name, cfg in UNDERLYINGS.items():
        sname = spot_names.get(name)
        est = PREV_CLOSE.get(sname)
        if est is None:
            snap = POOL.snapshot().get(sname) or {}
            est = snap.get("ltp")
        if est is None:
            print(f"[WARN] {name}: no previous close or LTP, cannot build band")
            continue

        rows, _ = build_band(masters, name, cfg, est)
        bands[name] = rows
        band_subs += [{"instrument_token": r["token"],
                       "exchange_segment": r["segment"]} for r in rows]

    total = len(idx_subs) + len(fut_subs) + len(band_subs)
    if total > MAX_TOTAL_SCRIPS:
        raise ValueError(f"{total} subscriptions exceeds {MAX_TOTAL_SCRIPS}. "
                         f"Reduce BAND in strategy_config.py")
    print(f"\n[SUB ] {total}/{MAX_TOTAL_SCRIPS} subscriptions "
          f"({len(band_subs)} band legs)")
    for i in range(0, len(band_subs), 100):
        client.subscribe(instrument_tokens=band_subs[i:i + 100])
        _time.sleep(1)

    POOL.start_roller(interval=1.0)

    # ---- wait for each session, then lock ------------------------------
    today = datetime.now(IST).date()
    pending = {}
    for name, cfg in UNDERLYINGS.items():
        if name not in bands:
            continue
        open_t = SESSION_OPEN[cfg["fo_segment"]]
        ref_bucket = datetime.combine(today, open_t, tzinfo=IST)
        pending[name] = {"cfg": cfg, "ref_bucket": ref_bucket,
                         "lock_at": ref_bucket + timedelta(minutes=1,
                                                           seconds=LOCK_DELAY_SEC)}
        print(f"[PLAN] {name}: reference candle {ref_bucket:%H:%M}, "
              f"lock at {pending[name]['lock_at']:%H:%M:%S}")

    print("\n[RUN ] waiting for session opens... Ctrl-C to stop\n")
    last_status = 0.0
    try:
        while True:
            now = datetime.now(IST)

            for name in list(pending):
                if now >= pending[name]["lock_at"]:
                    info = pending.pop(name)
                    kept, dropped = lock_underlying(
                        name, info["cfg"], bands[name],
                        spot_names[name], info["ref_bucket"])
                    if dropped and UNSUBSCRIBE_BAND_AFTER_LOCK:
                        for i in range(0, len(dropped), 100):
                            try:
                                client.un_subscribe(
                                    instrument_tokens=dropped[i:i + 100])
                            except Exception as e:
                                print(f"[LOCK] unsubscribe error: {e}")
                        print(f"       dropped {len(dropped)} band legs, "
                              f"trading {len(kept)}")

            if _time.time() - last_status > STATUS_EVERY:
                print_status()
                last_status = _time.time()

            _time.sleep(1)

    except KeyboardInterrupt:
        print("\n[RUN ] stopping")
        POOL.stop()
        print_status()
        with _state_lock:
            for lg in LEGS.values():
                for t in lg.trades:
                    print(f"  {lg.symbol:<26} {t['side']:<5} "
                          f"{t['in']:%H:%M} {t['entry']:>9,.2f} -> "
                          f"{t['out']:%H:%M} {t['exit']:>9,.2f}  "
                          f"{t['pnl']:>+10,.2f}")
        try:
            client.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
