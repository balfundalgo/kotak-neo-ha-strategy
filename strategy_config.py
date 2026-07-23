"""
Strategy configuration
======================
First-candle HA-close reference, stop-and-reverse.

Reference candle (Rule B, per underlying, independent)
------------------------------------------------------
The reference is the first FULLY-COMPLETE 1-minute candle at or after the
"effective start" for each underlying, where

    effective start = max( when you pressed START , that segment's open )

so:
  * start before the open  -> reference is the session-open candle
                              (09:00 for mcx_fo, 09:15 for nse_fo/bse_fo)
  * start after the open   -> reference is the first complete candle after
                              you started (e.g. start 10:00:30 -> 10:01-10:02)

Each underlying decides this on its own, so starting at 09:05 gives MCX a
first-candle-after-start reference while NIFTY/SENSEX still wait for 09:15.
ATM is (re)locked from the spot/future close of that same reference candle.
"""

from datetime import time

# ---------------------------------------------------------------------------
# Session open per segment.
# ---------------------------------------------------------------------------
SESSION_OPEN = {
    "nse_fo": time(9, 15),
    "bse_fo": time(9, 15),
    "mcx_fo": time(9, 0),
}

# Strikes either side of the estimated ATM to subscribe before locking.
#   band 5 -> 11 strikes x 2 sides x 3 underlyings = 66 legs (+3 spot)
BAND = 5

# Drop the unselected band legs once ATM is locked.
UNSUBSCRIBE_BAND_AFTER_LOCK = True

# Seconds after the reference minute closes before locking (roller buffer).
LOCK_DELAY_SEC = 5

# Fill price for paper trades on candle-close signals:
#   "close"    -> raw candle close (tradeable price)   [default]
#   "ha_close" -> Heikin Ashi close (synthetic)
# NOTE: target-profit exits always fill at the live LTP that triggered them,
# regardless of this setting, because a take-profit hits intra-candle.
FILL_PRICE = "close"

# Status table interval (seconds)
STATUS_EVERY = 60

# ---------------------------------------------------------------------------
# Per-script control
# ---------------------------------------------------------------------------
# For each underlying:
#   "CE" / "PE"      -> True to trade that side, False to skip it entirely
#   "target_points"  -> per-leg take-profit in the OPTION's own premium points.
#                       When an open leg's premium has moved this many points in
#                       your favour from entry, it books the profit at the live
#                       LTP and goes flat, then re-enters on the next signal.
#                       Set 0 (or None) to disable the target for that script.
#
# Same target applies to both CE and PE of an underlying. Toggles are per side,
# so you get six independent switches: NIFTY CE, NIFTY PE, SENSEX CE, SENSEX PE,
# CRUDEOILM CE, CRUDEOILM PE.
#   "lots"           -> number of lots per entry. Order quantity is
#                       lots x the exchange lot size (NIFTY 65, SENSEX 20,
#                       CRUDEOILM 10 - taken from the scrip master).
#                       A reversal sends 2 x this (close + open).
SCRIPT_CONFIG = {
    "NIFTY":     {"CE": True, "PE": True, "target_points": 0, "lots": 1},
    "SENSEX":    {"CE": True, "PE": True, "target_points": 0, "lots": 1},
    "CRUDEOILM": {"CE": True, "PE": True, "target_points": 0, "lots": 1},
}
