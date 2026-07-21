"""
Strategy configuration
======================
First-candle HA-close reference, stop-and-reverse.
"""

from datetime import time

# ---------------------------------------------------------------------------
# Session open per segment. The FIRST 1-minute candle of the session is the
# reference candle; signals start from the candle after it.
# ---------------------------------------------------------------------------
SESSION_OPEN = {
    "nse_fo": time(9, 15),
    "bse_fo": time(9, 15),
    "mcx_fo": time(9, 0),
}

# How many strikes either side of the estimated ATM to subscribe before the
# bell. We do not know the true ATM until the first candle closes, so we
# record candles for a band and pick from it afterwards.
#   band 5 -> 11 strikes x 2 sides x 3 underlyings = 66 legs (+3 spot)
BAND = 5

# Drop the unselected band legs once ATM is locked, keeping only the 6 traded
# legs plus spot. Set False to keep the whole band streaming.
UNSUBSCRIBE_BAND_AFTER_LOCK = True

# Seconds after the session's first minute before locking ATM. Small buffer so
# the roller has closed the first candle on every engine.
LOCK_DELAY_SEC = 5

# Fill price for paper trades:
#   "close"    -> raw candle close (tradeable price)   [default]
#   "ha_close" -> Heikin Ashi close (synthetic)
FILL_PRICE = "close"

# Status table interval (seconds)
STATUS_EVERY = 60
