"""
1-minute OHLC + Heikin Ashi candle engine
=========================================
Broker-agnostic. Feed it ticks, it emits closed 1-minute candles with
Heikin Ashi values attached.

Heikin Ashi formulas
--------------------
    HA_Close = (O + H + L + C) / 4
    HA_Open  = (prev_HA_Open + prev_HA_Close) / 2
               first candle -> (O + C) / 2
    HA_High  = max(H, HA_Open, HA_Close)
    HA_Low   = min(L, HA_Open, HA_Close)

NOTE ON WARM-UP
---------------
HA_Open is recursive, so a live engine started mid-session will not exactly
match a chart that computed HA from the start of the day. The difference
decays geometrically (halves every candle) and is negligible after roughly
15-20 candles, but early candles should NOT be traded on. Use
`engine.is_warm(n)` to gate entries, or seed with historical candles via
`seed(candles)` if a history API is available.
"""

import threading
from collections import deque
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

WARMUP_CANDLES = 20      # HA considered converged after this many closes


def minute_bucket(dt):
    """Floor a datetime to its minute."""
    return dt.replace(second=0, microsecond=0)


class CandleEngine:
    """
    One instrument. Thread-safe.

    on_tick(price)          -> feed a trade/quote price
    roll(now)               -> close the candle if its minute has elapsed
    on_candle_close(cb)     -> register callback, cb(name, candle)
    """

    def __init__(self, name, maxlen=500, precision=2):
        self.name = name
        self.precision = precision
        self._lock = threading.RLock()

        self.current = None          # in-progress candle
        self.candles = deque(maxlen=maxlen)   # closed candles
        self.last_price = None
        self.last_tick_at = None

        self._prev_ha_open = None
        self._prev_ha_close = None
        self._callbacks = []

    # -- registration -----------------------------------------------------
    def on_candle_close(self, cb):
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    # -- heikin ashi ------------------------------------------------------
    def _apply_ha(self, c):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4.0

        if self._prev_ha_open is None:
            ha_open = (c["open"] + c["close"]) / 2.0     # seed
        else:
            ha_open = (self._prev_ha_open + self._prev_ha_close) / 2.0

        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)

        self._prev_ha_open = ha_open
        self._prev_ha_close = ha_close

        c["ha_open"] = ha_open
        c["ha_high"] = ha_high
        c["ha_low"] = ha_low
        c["ha_close"] = ha_close
        c["ha_colour"] = "green" if ha_close >= ha_open else "red"
        # body/wick geometry - useful for entry rules later
        c["ha_body"] = abs(ha_close - ha_open)
        c["ha_upper_wick"] = ha_high - max(ha_open, ha_close)
        c["ha_lower_wick"] = min(ha_open, ha_close) - ha_low
        return c

    # -- feed -------------------------------------------------------------
    def on_tick(self, price, now=None, exch_time=None):
        """Feed one price. `now` defaults to wall-clock IST."""
        try:
            price = float(price)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None

        now = now or datetime.now(IST)
        bucket = minute_bucket(now)
        closed = None

        with self._lock:
            self.last_price = price
            self.last_tick_at = now

            if self.current is None:
                self.current = self._new_candle(bucket, price, exch_time)
                return None

            if bucket == self.current["bucket"]:
                c = self.current
                c["high"] = max(c["high"], price)
                c["low"] = min(c["low"], price)
                c["close"] = price
                c["ticks"] += 1
                c["exch_time"] = exch_time or c["exch_time"]
                return None

            if bucket > self.current["bucket"]:
                closed = self._close_current()
                self.current = self._new_candle(bucket, price, exch_time)
            # older tick -> ignore

        if closed:
            self._emit(closed)
        return closed

    def roll(self, now=None):
        """
        Close the in-progress candle if its minute has passed, even with no
        new tick. Keeps candles aligned to :00 for quiet instruments.
        """
        now = now or datetime.now(IST)
        bucket = minute_bucket(now)
        closed = None

        with self._lock:
            if self.current and bucket > self.current["bucket"]:
                closed = self._close_current()
                self.current = None

        if closed:
            self._emit(closed)
        return closed

    # -- internals --------------------------------------------------------
    def _new_candle(self, bucket, price, exch_time=None):
        return {
            "bucket": bucket, "open": price, "high": price,
            "low": price, "close": price, "ticks": 1,
            "exch_time": exch_time,
        }

    def _close_current(self):
        c = self.current
        c = self._apply_ha(c)
        self.candles.append(c)
        return c

    def _emit(self, candle):
        for cb in self._callbacks:
            try:
                cb(self.name, candle)
            except Exception as e:
                print(f"[ENGINE] callback error on {self.name}: {e}")

    # -- reads ------------------------------------------------------------
    def is_warm(self, n=WARMUP_CANDLES):
        with self._lock:
            return len(self.candles) >= n

    def last(self, n=1):
        with self._lock:
            if len(self.candles) < n:
                return None
            return dict(self.candles[-n])

    def history(self, n=20):
        with self._lock:
            return [dict(c) for c in list(self.candles)[-n:]]

    def seed(self, ohlc_list):
        """Pre-load historical 1-min OHLC so HA starts converged."""
        with self._lock:
            for c in ohlc_list:
                cc = dict(c)
                cc.setdefault("ticks", 0)
                cc.setdefault("exch_time", None)
                self.candles.append(self._apply_ha(cc))

    def snapshot(self):
        with self._lock:
            return {
                "name": self.name,
                "ltp": self.last_price,
                "last_tick_at": self.last_tick_at,
                "current": dict(self.current) if self.current else None,
                "closed": len(self.candles),
                "warm": len(self.candles) >= WARMUP_CANDLES,
                "last_candle": dict(self.candles[-1]) if self.candles else None,
            }


class EnginePool:
    """Holds one CandleEngine per instrument plus a roller thread."""

    def __init__(self, precision_map=None):
        self.engines = {}
        self.precision_map = precision_map or {}
        self._lock = threading.RLock()
        self._callbacks = []
        self._stop = threading.Event()
        self._thread = None

    def on_candle_close(self, cb):
        # guard against double-registration: a second START would otherwise
        # evaluate every candle twice and send duplicate orders
        if cb in self._callbacks:
            return
        self._callbacks.append(cb)
        with self._lock:
            for e in self.engines.values():
                e.on_candle_close(cb)

    def engine(self, name):
        with self._lock:
            if name not in self.engines:
                e = CandleEngine(name, precision=self.precision_map.get(name, 2))
                for cb in self._callbacks:
                    e.on_candle_close(cb)
                self.engines[name] = e
            return self.engines[name]

    def on_tick(self, name, price, exch_time=None):
        return self.engine(name).on_tick(price, exch_time=exch_time)

    def start_roller(self, interval=1.0):
        """Background thread that closes candles on the minute boundary."""
        self._stop.clear()          # allow restart after a previous stop()

        def _loop():
            while not self._stop.wait(interval):
                now = datetime.now(IST)
                with self._lock:
                    engines = list(self.engines.values())
                for e in engines:
                    try:
                        e.roll(now)
                    except Exception as ex:
                        print(f"[ROLLER] {e.name}: {ex}")

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def reset(self):
        """Stop the roller and drop all engines/callbacks - used between runs."""
        self.stop()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3)
        self._thread = None
        with self._lock:
            self.engines.clear()
        self._callbacks.clear()
        self._stop = threading.Event()

    def snapshot(self):
        with self._lock:
            return {n: e.snapshot() for n, e in self.engines.items()}
