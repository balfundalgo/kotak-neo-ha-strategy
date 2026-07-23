"""
Order router - PAPER / LIVE execution hook
==========================================
Every entry, reversal, target-exit and toggle-off flows through execute_order().

    PAPER  -> records the intended order only; the Leg books a synthetic fill.
    LIVE   -> assembles the Kotak Neo place_order payload and transmits it.

CRITICAL: execute_order() parses the broker response and returns status
"SENT" only when the order was actually ACCEPTED (an order number came back).
Anything else is "REJECTED". The strategy must not change a leg's position
unless the order was accepted - see Leg._route() in paper_strategy.py.

Each order carries a UNIQUE tag. Kotak treats the tag as the client order id
and rejects duplicates with "Client OrderID already exists".

Product defaults to MIS (intraday).
"""

import itertools
import threading
from datetime import datetime

from candle_engine import IST

# ---------------------------------------------------------------------------
MODE = "PAPER"                 # "PAPER" or "LIVE"
CLIENT = None                  # authenticated NeoAPI client
DEFAULT_PRODUCT = "MIS"        # intraday

ORDER_LOG = []
_log_lock = threading.Lock()
_listeners = []
_seq = itertools.count(1)

ACCEPTED_STATUSES = ("PAPER", "SENT", "COMPLETE", "TRADED", "OPEN")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def set_client(client):
    global CLIENT
    CLIENT = client


def set_mode(mode):
    global MODE
    MODE = "LIVE" if str(mode).upper() == "LIVE" else "PAPER"
    return MODE


def get_mode():
    return MODE


def on_order(cb):
    if cb not in _listeners:
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
# Unique client order id
# ---------------------------------------------------------------------------
def new_tag():
    """Unique per order. Kotak rejects duplicate tags as duplicate order ids."""
    stamp = int(datetime.now(IST).timestamp()) % 100000000
    return f"bf{stamp}{next(_seq):04d}"          # 14 chars


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
def build_payload(symbol, exchange_segment, action, quantity,
                  price=0.0, order_type="MKT", product=DEFAULT_PRODUCT):
    is_limit = order_type.upper() in ("L", "LIMIT")
    return {
        "exchange_segment": exchange_segment,
        "product": product,
        "price": str(price) if is_limit else "0",
        "order_type": "L" if is_limit else "MKT",
        "quantity": str(int(quantity)),
        "validity": "DAY",
        "trading_symbol": symbol,
        "transaction_type": "B" if action.upper() == "BUY" else "S",
        "amo": "NO",
        "disclosed_quantity": "0",
        "market_protection": "0",
        "pf": "N",
        "trigger_price": "0",
        "tag": new_tag(),                     # UNIQUE per order
    }


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------
def parse_response(resp):
    """
    Decide whether the broker ACCEPTED the order.
    Returns (accepted: bool, order_id: str|None, message: str).

    Accepted looks like : {'nOrdNo': '2607...', 'stat': 'Ok', 'stCode': 200}
    Rejected looks like : {'stCode': 32, 'errMsg': 'error from core',
                           'stat': 'Put Order Response : ... Failed : ...'}
    """
    if resp is None:
        return False, None, "no response from broker"
    if not isinstance(resp, dict):
        return False, None, str(resp)[:300]

    if "error" in resp:
        errs = resp.get("error")
        if isinstance(errs, list):
            msg = "; ".join(str(e.get("message", e)) if isinstance(e, dict)
                            else str(e) for e in errs)
        else:
            msg = str(errs)
        return False, None, msg[:300]

    data = resp.get("data") if isinstance(resp.get("data"), dict) else resp

    oid = (data.get("nOrdNo") or data.get("orderId")
           or data.get("order_id") or data.get("orderNumber"))
    stat = str(data.get("stat") or data.get("status") or "")
    err = (data.get("errMsg") or data.get("emsg")
           or data.get("errorMessage") or "")
    st_code = data.get("stCode")

    failed = False
    if err:
        failed = True
    if "fail" in stat.lower() or "reject" in stat.lower():
        failed = True
    if st_code is not None:
        try:
            if int(st_code) != 200:
                failed = True
        except (TypeError, ValueError):
            pass
    if not oid:
        failed = True

    message = (str(err) or stat or str(resp))[:300]
    return (not failed), (str(oid) if oid else None), message


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def execute_order(symbol, exchange_segment, action, quantity,
                  price=0.0, order_type="MKT", product=DEFAULT_PRODUCT,
                  reason=""):
    """
    Route one order. Returns the order dict; check order["status"] - only
    "PAPER" or "SENT" mean the strategy may change the leg's position.
    """
    ts = datetime.now(IST)
    payload = build_payload(symbol, exchange_segment, action, quantity,
                            price, order_type, product)
    order = {
        "time": ts, "mode": MODE, "symbol": symbol, "action": action.upper(),
        "qty": int(quantity), "product": product, "segment": exchange_segment,
        "price": price, "reason": reason, "payload": payload,
        "status": "PAPER", "broker_order_id": None, "message": "",
    }

    if MODE == "LIVE":
        try:
            resp = broker_place_order(payload)
            ok, oid, message = parse_response(resp)
            order["status"] = "SENT" if ok else "REJECTED"
            order["broker_order_id"] = oid
            order["message"] = message
        except Exception as e:
            order["status"] = "ERROR"
            order["message"] = str(e)[:300]

    _record(order)
    ids = f"  id {order['broker_order_id']}" if order["broker_order_id"] else ""
    msg = f"  {order['message']}" if order["message"] else ""
    print(f"{ts:%H:%M:%S}  [ORDER/{MODE}] {order['action']} {order['qty']} "
          f"{symbol} {product} {order['status']}{ids}{msg}")
    return order


def was_accepted(order):
    return bool(order) and order.get("status") in ACCEPTED_STATUSES


def broker_place_order(payload):
    """
    LIVE transmit. The payload above is fully assembled and validated.
    Uncomment the ONE line marked below to send orders to Kotak.
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
# Broker order feed (fill / reject status)
# ---------------------------------------------------------------------------
def handle_order_update(msg):
    """Update a logged order's status from the Kotak order feed."""
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
                    rej = data.get("rejReason") or data.get("rejreason")
                    if rej:
                        o["message"] = str(rej)[:300]
                    break
        for cb in list(_listeners):
            try:
                cb({"__update__": True})
            except Exception:
                pass
    except Exception as e:
        print(f"[ORDER] update parse error: {e}")
