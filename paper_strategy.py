"""
Paper strategy - first-candle HA reference, stop-and-reverse
============================================================

Reference candle (Rule B, per underlying, independent)
------------------------------------------------------
When you press START, each underlying computes

    effective start = max( now , that segment's session open )

and takes as its reference the first FULLY-COMPLETE 1-minute candle at or
after that. So:
  * start before the open -> reference is the session-open candle
                             (09:00 mcx_fo, 09:15 nse_fo/bse_fo)
  * start after the open  -> reference is the first complete candle after
                             you started (start 10:00:30 -> candle 10:01-10:02)
Each underlying decides independently. ATM is (re)locked from the spot/future
close of that same reference candle, so a 10:00 start trades strikes that are
ATM at 10:00, not at the open.

Trading (unchanged)
-------------------
From the candle AFTER the reference, on every closed candle:
    HA close > reference -> target LONG
    HA close < reference -> target SHORT
Stop-and-reverse: first entry 1 lot, each reversal 2 lots. CE and PE are
independent.

Per-script controls (strategy_config.SCRIPT_CONFIG)
---------------------------------------------------
  * Per-side on/off: six switches (NIFTY CE/PE, SENSEX CE/PE, CRUDEOILM CE/PE).
    A disabled side is never locked and never traded.
  * Per-script target in premium points: a take-profit that sits ON TOP of the
    stop-and-reverse. When an open leg's premium moves target_points in your
    favour it books profit at the live LTP, goes flat, and re-enters on the
    next signal. Checked live on every tick, not just at candle close.

Signals use the Heikin Ashi close; HA close = (O+H+L+C)/4 is exact from the
first candle, so no warm-up is needed for this strategy.

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
from candle_engine import EnginePool, IST, minute_bucket
from strategy_config import (
    SESSION_OPEN, BAND, UNSUBSCRIBE_BAND_AFTER_LOCK,
    LOCK_DELAY_SEC, FILL_PRICE, STATUS_EVERY, SCRIPT_CONFIG,
)

POOL = EnginePool()
PREV_CLOSE = {}          # name -> previous close from the feed
LEGS = {}                # symbol -> Leg (only the locked legs)
_state_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Position / paper book
# ---------------------------------------------------------------------------
class Leg:
    def __init__(self, symbol, token, segment, underlying, side, strike, lot,
                 target_points=None):
        self.symbol = symbol
        self.token = token
        self.segment = segment
        self.underlying = underlying
        self.side = side                # CE / PE
        self.strike = strike
        self.lot = int(lot)             # exchange lot size = P&L multiplier
        self.target_points = target_points  # premium points, or None

        self.reference = None
        self.state = "FLAT"             # FLAT / LONG / SHORT
        self.entry_price = None
        self.entry_time = None
        self.realized = 0.0
        self.trades = []
        self.last_price = None
        self.active = True              # per-side live switch (future GUI use)
        self._skip_bucket = None        # candle we target-exited in; no re-entry
        self._pos_bucket = None         # candle the current position opened in

    # -- paper fills ----------------------------------------------------
    def _open(self, side, px, ts):
        self.state = side
        self.entry_price = px
        self.entry_time = ts
        self._pos_bucket = minute_bucket(ts)   # candle this position opened in

    def _close(self, px, ts, reason=""):
        if self.state == "LONG":
            pnl = (px - self.entry_price) * self.lot
        elif self.state == "SHORT":
            pnl = (self.entry_price - px) * self.lot
        else:
            return 0.0
        self.realized += pnl
        self.trades.append({
            "side": self.state, "entry": self.entry_price, "exit": px,
            "in": self.entry_time, "out": ts, "pnl": pnl, "reason": reason,
        })
        self.state = "FLAT"
        self.entry_price = None
        self.entry_time = None
        return pnl

    def unrealized(self, px=None):
        px = px if px is not None else self.last_price
        if px is None or self.state == "FLAT":
            return 0.0
        if self.state == "LONG":
            return (px - self.entry_price) * self.lot
        return (self.entry_price - px) * self.lot

    # -- order routing (PAPER/LIVE via order_router) --------------------
    def _route(self, action, qty, price, reason):
        try:
            import order_router
            order_router.execute_order(
                symbol=self.symbol, exchange_segment=self.segment,
                action=action, quantity=qty, price=price,
                order_type="MKT", reason=reason)
        except Exception as e:
            print(f"[ORDER] route error on {self.symbol}: {e}")

    # -- live tick: update LTP and check the profit target --------------
    def on_price(self, px):
        try:
            px = float(px)
        except (TypeError, ValueError):
            return
        self.last_price = px

        if (self.state == "FLAT" or not self.active
                or not self.target_points or self.target_points <= 0):
            return

        move = (px - self.entry_price) if self.state == "LONG" \
            else (self.entry_price - px)
        if move >= self.target_points:
            ts = datetime.now(IST)
            side = self.state
            # skip re-entry in the candle this position is currently in
            self._skip_bucket = minute_bucket(ts)
            close_act = "SELL" if side == "LONG" else "BUY"
            self._route(close_act, self.lot, px, "TARGET")
            pnl = self._close(px, ts, reason="TARGET")
            print(f"{ts:%H:%M:%S}  {self.symbol:<26} TARGET {side} "
                  f"+{move:,.2f}pt >= {self.target_points:g}  exit @ "
                  f"{px:>9,.2f}  booked {pnl:>+10,.2f}")

    # -- candle close: stop-and-reverse signal --------------------------
    def evaluate(self, candle):
        if self.reference is None or not self.active:
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
            return

        if target == self.state:
            return

        # Don't re-enter inside the same candle a target-exit just fired in.
        if self.state == "FLAT" and candle["bucket"] == self._skip_bucket:
            return

        prev = self.state
        pnl = self._close(fill, ts, reason="REVERSE") if prev != "FLAT" else 0.0
        self._open(target, fill, ts)

        lots = 1 if prev == "FLAT" else 2
        qty = self.lot * lots
        act = "BUY" if target == "LONG" else "SELL"
        tag = "ENTRY" if prev == "FLAT" else f"REVERSE {prev}->{target}"
        pnl_s = f" | closed P&L {pnl:>+10,.2f}" if prev != "FLAT" else ""

        self._route(act, qty, fill, tag)

        print(f"{ts:%H:%M}  {self.symbol:<26} HA {ha:>9,.2f} vs ref "
              f"{self.reference:>9,.2f}  {act} {qty:>5} ({lots} lot"
              f"{'s' if lots > 1 else ''}) @ {fill:>9,.2f}  [{tag}]{pnl_s}")

    # -- live toggle-off: immediate exit at LTP -------------------------
    def force_close(self, px=None, ts=None):
        if self.state == "FLAT":
            return 0.0
        px = px if px is not None else self.last_price
        if px is None:
            return 0.0
        ts = ts or datetime.now(IST)
        close_act = "SELL" if self.state == "LONG" else "BUY"
        self._route(close_act, self.lot, px, "TOGGLE-OFF")
        pnl = self._close(px, ts, reason="TOGGLE-OFF")
        print(f"{ts:%H:%M:%S}  {self.symbol:<26} TOGGLE-OFF exit @ "
              f"{px:>9,.2f}  booked {pnl:>+10,.2f}")
        return pnl


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

        # live profit-target check for a traded leg
        with _state_lock:
            leg = LEGS.get(name)
        if leg is not None:
            leg.on_price(price)


def on_candle(name, candle):
    with _state_lock:
        leg = LEGS.get(name)
    if leg:
        leg.evaluate(candle)


def apply_live_toggle(symbol, active):
    """GUI per-side switch. Turning a leg off exits it immediately at LTP."""
    with _state_lock:
        leg = LEGS.get(symbol)
    if leg is None:
        return
    was = leg.active
    leg.active = bool(active)
    if was and not leg.active:
        leg.force_close()


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
# Reference-candle timing (Rule B)
# ---------------------------------------------------------------------------
def ceil_minute(dt):
    """Smallest minute boundary >= dt (equal to dt if already on a boundary)."""
    b = dt.replace(second=0, microsecond=0)
    return b if dt == b else b + timedelta(minutes=1)


def plan_reference(name, cfg, algo_ready, today):
    """
    Decide the reference candle bucket and lock time for one underlying.
    Returns (ref_bucket, lock_at, late).
    """
    open_t = SESSION_OPEN[cfg["fo_segment"]]
    session_open_dt = datetime.combine(today, open_t, tzinfo=IST)
    effective = max(algo_ready, session_open_dt)
    ref_bucket = ceil_minute(effective)
    late = algo_ready > session_open_dt
    lock_at = ref_bucket + timedelta(minutes=1, seconds=LOCK_DELAY_SEC)
    return ref_bucket, lock_at, late


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def first_candle(engine, ref_bucket):
    """
    Candle stamped exactly at ref_bucket. Falls back to the earliest candle
    after it (with a late flag) if that exact one is missing.
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
    """Lock ATM from the reference candle and set references. Returns
    (kept_symbols, dropped_subs)."""
    spot_engine = POOL.engines.get(spot_name)
    if spot_engine is None:
        print(f"[LOCK] {underlying}: no spot engine, skipping")
        return [], []

    spot_c, late = first_candle(spot_engine, ref_bucket)
    if spot_c is None:
        print(f"[LOCK] {underlying}: no spot candle at {ref_bucket:%H:%M} yet, "
              f"skipping")
        return [], []
    if late:
        print(f"[LOCK] {underlying}: exact {ref_bucket:%H:%M} spot candle "
              f"missing, using {spot_c['bucket']:%H:%M}")

    spot_px = spot_c["close"]
    strikes = sorted({r["strike"] for r in band_rows})
    if not strikes:
        return [], []
    atm = min(strikes, key=lambda s: abs(s - spot_px))

    scfg = SCRIPT_CONFIG.get(underlying, {})
    target = scfg.get("target_points") or 0
    target = target if target > 0 else None

    print(f"\n[LOCK] {underlying}: reference candle {ref_bucket:%H:%M} "
          f"spot close {spot_px:,.2f} -> ATM {atm:,.0f}"
          + (f" | target {target:g}pt" if target else " | no target"))

    kept, dropped = [], []
    for r in band_rows:
        keep = (r["strike"] == atm) and bool(scfg.get(r["side"], True))
        if not keep:
            dropped.append({"instrument_token": r["token"],
                            "exchange_segment": r["segment"]})
            if r["strike"] == atm and not scfg.get(r["side"], True):
                print(f"       {r['side']} {r['strike']:,.0f}: disabled in "
                      f"config, not traded")
            continue

        eng = POOL.engines.get(r["symbol"])
        if eng is None:
            print(f"       {r['side']} {r['strike']:,.0f}: {r['symbol']} "
                  f"- no candles, skipped")
            continue

        c, clate = first_candle(eng, ref_bucket)
        if c is None:
            print(f"       {r['side']} {r['strike']:,.0f}: {r['symbol']} "
                  f"- no candle, skipped")
            continue
        if clate:
            print(f"       [WARN] {r['symbol']}: no ticks at "
                  f"{ref_bucket:%H:%M}, reference from {c['bucket']:%H:%M}")

        leg = Leg(r["symbol"], r["token"], r["segment"], underlying,
                  r["side"], r["strike"], r["lot"], target_points=target)
        leg.reference = c["ha_close"]
        with _state_lock:
            LEGS[r["symbol"]] = leg
        kept.append(r["symbol"])

        print(f"       {r['side']} {r['strike']:,.0f}: {r['symbol']:<26} "
              f"ref HA close {leg.reference:>9,.2f}  (O {c['open']:,.2f} "
              f"H {c['high']:,.2f} L {c['low']:,.2f} C {c['close']:,.2f})  "
              f"lot {leg.lot}")

    return kept, dropped


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def print_status():
    with _state_lock:
        legs = list(LEGS.values())
    if not legs:
        return
    print(f"\n{'=' * 112}")
    print(f"{datetime.now(IST):%H:%M:%S}  PAPER BOOK")
    print(f"{'instrument':<26}{'ref':>10}{'last':>10}{'state':>7}"
          f"{'entry':>10}{'tgt':>7}{'qty':>7}{'realized':>12}"
          f"{'unreal':>12}{'trades':>8}")
    print("-" * 112)
    tot_r = tot_u = 0.0
    for lg in sorted(legs, key=lambda x: (x.underlying, x.side)):
        u = lg.unrealized()
        tot_r += lg.realized
        tot_u += u
        tgt = f"{lg.target_points:g}" if lg.target_points else "-"
        print(f"{lg.symbol:<26}{lg.reference or 0:>10,.2f}"
              f"{lg.last_price or 0:>10,.2f}{lg.state:>7}"
              f"{lg.entry_price or 0:>10,.2f}{tgt:>7}"
              f"{lg.lot if lg.state != 'FLAT' else 0:>7}"
              f"{lg.realized:>+12,.2f}{u:>+12,.2f}{len(lg.trades):>8}")
    print("-" * 112)
    print(f"{'TOTAL':<76}{tot_r:>+12,.2f}{tot_u:>+12,.2f}"
          f"{'  net ' + format(tot_r + tot_u, '+,.2f'):>20}")
    print(f"{'=' * 112}\n")


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

    print("\n[PREV] waiting for spot prices / previous close...")
    deadline = _time.time() + 30
    while _time.time() < deadline:
        if all(spot_names[u] in PREV_CLOSE or spot_names[u] in POOL.engines
               for u in spot_names):
            break
        _time.sleep(0.5)

    # ---- band subscriptions (centre on live LTP, else previous close) --
    print()
    bands, band_subs = {}, []
    for name, cfg in UNDERLYINGS.items():
        sname = spot_names.get(name)
        snap = POOL.snapshot().get(sname) or {}
        est = snap.get("ltp") or PREV_CLOSE.get(sname)
        if est is None:
            print(f"[WARN] {name}: no LTP or previous close, cannot build band")
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

    # ---- plan each underlying's reference candle (Rule B) --------------
    algo_ready = datetime.now(IST)
    today = algo_ready.date()
    pending = {}
    for name, cfg in UNDERLYINGS.items():
        if name not in bands:
            continue
        ref_bucket, lock_at, late = plan_reference(name, cfg, algo_ready, today)
        pending[name] = {"cfg": cfg, "ref_bucket": ref_bucket,
                         "lock_at": lock_at}
        tag = "LATE-START" if late else "session-open"
        print(f"[PLAN] {name}: reference candle {ref_bucket:%H:%M} ({tag}), "
              f"lock at {lock_at:%H:%M:%S}")

    print("\n[RUN ] waiting for reference candles... Ctrl-C to stop\n")
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
                        print(f"       dropped {len(dropped)} legs, "
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
                          f"{t['pnl']:>+10,.2f}  [{t.get('reason','')}]")
        try:
            client.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
