"""
Order router - PAPER / LIVE execution hook
==========================================
Every entry, reversal, target-exit and toggle-off in the strategy flows through
execute_order(). The router is mode-aware:

    PAPER  -> records the intended order only; the Leg books a synthetic fill.
    LIVE   -> assembles the full Kotak Neo place_order payload and calls
              broker_place_order() to transmit it.

The payload is fully assembled and validated here. broker_place_order() holds
the finished payload and ONE commented line that actually sends the order to
your Kotak Neo account. Uncomment that line to go live. Until then, LIVE mode
assembles and logs the exact order (and marks it ERROR) without transmitting.

Product defaults to MIS (intraday).
"""

import threading
from datetime import datetime

from candle_engine import IST

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------
MODE = "PAPER"                 # "PAPER" or "LIVE"
CLIENT = None                  # authenticated NeoAPI client (set after login)
DEFAULT_PRODUCT = "MIS"        # intraday

ORDER_LOG = []                 # chronological list of order dicts
_log_lock = threading.Lock()
_listeners = []                # GUI callbacks: cb(order_dict)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def set_client(client):
    global CLIENT
    CLIENT = client


def set_mode(mode):
    """Set PAPER or LIVE. Returns the normalised mode."""
    global MODE
    MODE = "LIVE" if str(mode).upper() == "LIVE" else "PAPER"
    return MODE


def get_mode():
    return MODE


def on_order(cb):
    """Register a GUI listener called with each recorded order."""
    _listeners.append(cb)


def orders_snapshot():
    with _log_lock:
        return [dict(o) for o in ORDER_LOG]


def _record(order):
    with _log_lock:
        ORDER_LOG.append(order)
    for cb in list(_listeners):
        try:
            cb(order)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Payload assembly (Kotak Neo place_order)
# ---------------------------------------------------------------------------
def build_payload(symbol, exchange_segment, action, quantity,
                  price=0.0, order_type="MKT", product=DEFAULT_PRODUCT):
    """
    Assemble the Kotak Neo place_order payload. Complete and validated.

    order_type : "MKT" (market) or "L" (limit)
    action     : "BUY" / "SELL"  -> transaction_type "B" / "S"
    product    : "MIS" (intraday, default), "NRML", "CNC", ...
    """
    is_limit = order_type.upper() in ("L", "LIMIT")
    return {
        "exchange_segment": exchange_segment,          # nse_fo / bse_fo / mcx_fo
        "product": product,                            # MIS
        "price": str(price) if is_limit else "0",
        "order_type": "L" if is_limit else "MKT",
        "quantity": str(int(quantity)),
        "validity": "DAY",
        "trading_symbol": symbol,                      # e.g. CRUDEOILM17AUG268350CE
        "transaction_type": "B" if action.upper() == "BUY" else "S",
        "amo": "NO",
        "disclosed_quantity": "0",
        "market_protection": "0",
        "pf": "N",
        "trigger_price": "0",
        "tag": "balfund",
    }


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute_order(symbol, exchange_segment, action, quantity,
                  price=0.0, order_type="MKT", product=DEFAULT_PRODUCT,
                  reason=""):
    """
    Route one order. In PAPER, records intent. In LIVE, assembles the payload
    and transmits via broker_place_order(). Always returns the order dict.
    """
    ts = datetime.now(IST)
    payload = build_payload(symbol, exchange_segment, action, quantity,
                            price, order_type, product)
    order = {
        "time": ts, "mode": MODE, "symbol": symbol, "action": action.upper(),
        "qty": int(quantity), "product": product, "segment": exchange_segment,
        "price": price, "reason": reason, "payload": payload,
        "status": "PAPER" if MODE == "PAPER" else "SENT",
        "broker_order_id": None, "message": "",
    }

    if MODE == "LIVE":
        try:
            resp = broker_place_order(payload)
            order["status"] = "SENT"
            order["message"] = (str(resp)[:300] if resp is not None else "")
            if isinstance(resp, dict):
                data = resp.get("data") or {}
                order["broker_order_id"] = (
                    data.get("orderId") or data.get("nOrdNo")
                    or data.get("order_id") or data.get("orderNumber"))
        except Exception as e:
            order["status"] = "ERROR"
            order["message"] = str(e)

    _record(order)
    ids = f"  id {order['broker_order_id']}" if order["broker_order_id"] else ""
    msg = f"  {order['message']}" if order["message"] else ""
    print(f"{ts:%H:%M:%S}  [ORDER/{MODE}] {order['action']} {order['qty']} "
          f"{symbol} {product} {order['status']}{ids}{msg}")
    return order


def broker_place_order(payload):
    """
    LIVE transmit. The payload above is fully assembled and validated.

    To go live, uncomment the ONE line marked below. It sends the order to your
    Kotak Neo account using the authenticated client. Until it is uncommented,
    LIVE mode raises here so nothing is transmitted and the order is logged with
    an ERROR status showing the exact payload that WOULD have been sent.
    """
    if CLIENT is None:
        raise RuntimeError("No authenticated client set (call set_client()).")

    # ======================================================================
    # >>> LIVE TRANSMIT - uncomment the next line to send the order to Kotak
    return CLIENT.place_order(**payload)
    # ======================================================================

    raise NotImplementedError(
        "LIVE transmit is disabled: uncomment CLIENT.place_order(**payload) "
        "in order_router.broker_place_order() to actually send orders."
    )


# ---------------------------------------------------------------------------
# Broker order-feed updates (fill / reject status)
# ---------------------------------------------------------------------------
def handle_order_update(msg):
    """
    Parse a Kotak order-feed message and update the matching order's status.
    Wire this to the SDK's order feed (see the marked line in app.py). The
    field names below cover the common Kotak Neo order-update shapes; adjust to
    your account's exact payload if needed.
    """
    try:
        data = msg.get("data", msg) if isinstance(msg, dict) else {}
        if isinstance(data, list):
            data = data[0] if data else {}
        oid = (data.get("nOrdNo") or data.get("orderId")
               or data.get("order_id") or data.get("orderNumber"))
        status = (data.get("ordSt") or data.get("status")
                  or data.get("orderStatus") or "")
        if not oid:
            return
        with _log_lock:
            for o in reversed(ORDER_LOG):
                if str(o.get("broker_order_id")) == str(oid):
                    o["status"] = str(status).upper() or o["status"]
                    o["message"] = str(data.get("rejReason") or "")[:300]
                    break
        for cb in list(_listeners):
            try:
                cb({"__update__": True})
            except Exception:
                pass
    except Exception as e:
        print(f"[ORDER] update parse error: {e}")
