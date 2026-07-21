"""
Kotak Neo - ATM option chain resolver
=====================================
Resolves ATM CE + PE tokens for NIFTY (nse_fo), SENSEX (bse_fo) and
CRUDEOILM (mcx_fo), then streams their live LTPs.

Flow:
    login -> subscribe spot -> read spot LTP -> resolve ATM strike
          -> subscribe ATM CE/PE -> stream

Usage:
    python option_chain.py --inspect    # dump scrip-master columns only
    python option_chain.py              # full run

Note on ATM source:
    NIFTY / SENSEX  -> index spot (nse_cm | bse_cm)
    CRUDEOILM       -> near-month FUTURES price (MCX options are on futures,
                       there is no index to read)
"""

import sys
import time
import argparse
from datetime import datetime, timezone, timedelta

import pandas as pd

from kotak_ws_base import login, LTP, get_ltp, _lock, _ALIAS

IST = timezone(timedelta(hours=5, minutes=30))

# nse_fo / cde_fo store expiry with this offset; mcx_fo / bse_fo are direct epoch
NSE_EPOCH_OFFSET = 315511200

MAX_TOTAL_SCRIPS = 200


# ---------------------------------------------------------------------------
# What to resolve
# ---------------------------------------------------------------------------
UNDERLYINGS = {
    "NIFTY": {
        "fo_segment": "nse_fo",
        "spot_type": "index",
        "spot_name": "Nifty 50",
        "spot_segment": "nse_cm",
    },
    "SENSEX": {
        "fo_segment": "bse_fo",
        "spot_type": "index",
        "spot_name": "SENSEX",
        "spot_segment": "bse_cm",
    },
    "CRUDEOILM": {
        "fo_segment": "mcx_fo",
        "spot_type": "future",      # ATM derived from near-month futures LTP
        "spot_name": "CRUDEOILM",
        "spot_segment": "mcx_fo",
    },
}


# ---------------------------------------------------------------------------
# Scrip master helpers
# ---------------------------------------------------------------------------
def load_scrip_master(client, segment):
    """scrip_master() returns a CSV URL - download and parse it."""
    url = client.scrip_master(exchange_segment=segment)
    if not isinstance(url, str) or not url.startswith("http"):
        raise RuntimeError(f"{segment}: unexpected scrip_master response: {url}")
    print(f"[CSV] {segment}: {url}")
    df = pd.read_csv(url, low_memory=False)
    df.columns = [c.strip().strip(";") for c in df.columns]   # headers can carry stray ';'
    return df


def find_col(df, *candidates, required=True, contains=None):
    """Locate a column tolerant of naming/casing differences between segments."""
    lower = {c.lower(): c for c in df.columns}
    for cand in candidates:
        if cand.lower() in lower:
            return lower[cand.lower()]
    if contains:
        for c in df.columns:
            if contains.lower() in c.lower():
                return c
    if required:
        raise KeyError(f"none of {candidates} found in columns: {list(df.columns)}")
    return None


def to_expiry_date(epoch, segment):
    """lExpiryDate -> date. nse_fo/cde_fo carry an offset; others are direct."""
    epoch = float(epoch)
    if segment in ("nse_fo", "cde_fo"):
        epoch += NSE_EPOCH_OFFSET
    return datetime.fromtimestamp(epoch, IST).date()


def option_rows(df, segment, underlying):
    """
    Filter the scrip master down to option contracts for one underlying.

    NOTE: pInstType is NOT consistent across exchanges --
        nse_fo -> OPTIDX / OPTSTK
        bse_fo -> IO (index option) / SO (stock option)
        mcx_fo -> OPTFUT / COM ...
    So options are identified by pOptionType being CE/PE, which holds
    on every segment, rather than by an instrument-type prefix.
    """
    c_sym = find_col(df, "pSymbolName", contains="symbolname")
    c_opt = find_col(df, "pOptionType", contains="optiontype")
    c_strike = find_col(df, "dStrikePrice", "pStrikePrice", contains="strike")
    c_exp = find_col(df, "lExpiryDate", contains="expiry")
    c_tok = find_col(df, "pSymbol", contains="symbol")
    c_trd = find_col(df, "pTrdSymbol", contains="trdsymbol")
    c_lot = find_col(df, "lLotSize", required=False, contains="lotsize")
    c_prec = find_col(df, "lPrecision", required=False, contains="precision")
    c_inst = find_col(df, "pInstType", required=False, contains="insttype")

    opt = df[
        (df[c_sym].astype(str).str.upper().str.strip() == underlying)
        & (df[c_opt].astype(str).str.upper().str.strip().isin(["CE", "PE"]))
    ].copy()

    if opt.empty:
        return opt, {}

    # dStrikePrice is stored scaled by 10**lPrecision (e.g. 374000 -> 3740.00)
    strike = pd.to_numeric(opt[c_strike], errors="coerce")
    if c_prec:
        prec = pd.to_numeric(opt[c_prec], errors="coerce").fillna(2).clip(lower=0)
        opt["_strike"] = strike / (10 ** prec)
    else:
        opt["_strike"] = strike / 100.0

    opt["_opt"] = opt[c_opt].astype(str).str.upper().str.strip()
    opt["_expiry"] = opt[c_exp].apply(lambda e: to_expiry_date(e, segment))
    opt = opt.dropna(subset=["_strike"])

    cols = {"token": c_tok, "trd": c_trd, "lot": c_lot, "inst": c_inst}
    return opt, cols


def normalise_strikes(opt, spot):
    """
    Kotak stores strikes in paise for some segments. If the strike scale is
    wildly off versus spot, rescale rather than silently picking a wrong ATM.
    """
    median = opt["_strike"].median()
    if spot and (median > spot * 10 or median < spot / 10):
        print(f"      [WARN] strike scale looks wrong: median {median:,.2f} "
              f"vs spot {spot:,.2f} - verify lPrecision handling")
    return opt


# ---------------------------------------------------------------------------
# Spot resolution
# ---------------------------------------------------------------------------
def near_month_future(df, segment, underlying):
    """Nearest-expiry futures contract - used as the ATM reference for MCX."""
    c_sym = find_col(df, "pSymbolName", contains="symbolname")
    c_inst = find_col(df, "pInstType", contains="insttype")
    c_exp = find_col(df, "lExpiryDate", contains="expiry")
    c_tok = find_col(df, "pSymbol", contains="symbol")
    c_trd = find_col(df, "pTrdSymbol", contains="trdsymbol")

    c_opt = find_col(df, "pOptionType", contains="optiontype")
    fut = df[
        (df[c_sym].astype(str).str.upper().str.strip() == underlying)
        & (~df[c_opt].astype(str).str.upper().str.strip().isin(["CE", "PE"]))
        & (df[c_inst].astype(str).str.upper().str.contains("FUT", na=False))
    ].copy()
    if fut.empty:
        return None

    fut["_expiry"] = fut[c_exp].apply(lambda e: to_expiry_date(e, segment))
    today = datetime.now(IST).date()
    fut = fut[fut["_expiry"] >= today].sort_values("_expiry")
    if fut.empty:
        return None

    row = fut.iloc[0]
    return {
        "instrument_token": str(row[c_tok]),
        "exchange_segment": segment,
        "trd": str(row[c_trd]),
        "expiry": row["_expiry"],
    }


def wait_for_ltp(names, timeout=25):
    """Block until every name has a price in the cache (or timeout)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = {n: get_ltp(n) for n in names}
        if all(v is not None for v in got.values()):
            return got
        time.sleep(0.5)
    return {n: get_ltp(n) for n in names}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true",
                    help="print scrip-master columns and exit")
    args = ap.parse_args()

    client = login()

    masters = {}
    for seg in sorted({u["fo_segment"] for u in UNDERLYINGS.values()}):
        masters[seg] = load_scrip_master(client, seg)

    if args.inspect:
        for seg, df in masters.items():
            print(f"\n===== {seg} : {len(df):,} rows =====")
            print("COLUMNS:", list(df.columns))
            print(df.head(3).to_string())
        return

    # ---- 1. subscribe spot instruments -------------------------------------
    spot_subs_idx, spot_subs_scrip, spot_names = [], [], {}

    for name, cfg in UNDERLYINGS.items():
        if cfg["spot_type"] == "index":
            spot_subs_idx.append({
                "instrument_token": cfg["spot_name"],
                "exchange_segment": cfg["spot_segment"],
            })
            spot_names[name] = cfg["spot_name"]
        else:
            fut = near_month_future(masters[cfg["fo_segment"]],
                                    cfg["fo_segment"], name)
            if not fut:
                print(f"[WARN] {name}: no futures contract found; skipping")
                continue
            print(f"[FUT ] {name}: {fut['trd']} exp {fut['expiry']} "
                  f"token {fut['instrument_token']}")
            with _lock:
                _ALIAS[fut["instrument_token"]] = fut["trd"]   # seed alias map
            spot_subs_scrip.append({
                "instrument_token": fut["instrument_token"],
                "exchange_segment": fut["exchange_segment"],
            })
            spot_names[name] = fut["trd"]

    if spot_subs_idx:
        client.subscribe(instrument_tokens=spot_subs_idx, isIndex=True)
    if spot_subs_scrip:
        client.subscribe(instrument_tokens=spot_subs_scrip)

    print("[SPOT] waiting for prices...")
    spots = wait_for_ltp(list(spot_names.values()))
    for k, v in spot_names.items():
        print(f"[SPOT] {k:<10} {v:<22} {spots.get(v)}")

    # ---- 2. resolve ATM CE/PE ---------------------------------------------
    option_subs = []
    today = datetime.now(IST).date()

    for name, cfg in UNDERLYINGS.items():
        seg = cfg["fo_segment"]
        spot = spots.get(spot_names.get(name))
        if spot is None:
            print(f"[WARN] {name}: no spot price, skipping")
            continue

        opt, cols = option_rows(masters[seg], seg, name)
        if opt.empty:
            print(f"[WARN] {name}: no option contracts found in {seg}")
            continue

        opt = normalise_strikes(opt, spot)

        future_exp = sorted(e for e in opt["_expiry"].unique() if e >= today)
        if not future_exp:
            print(f"[WARN] {name}: no unexpired contracts")
            continue
        expiry = future_exp[0]

        chain = opt[opt["_expiry"] == expiry]
        strikes = sorted(chain["_strike"].unique())
        atm = min(strikes, key=lambda s: abs(s - spot))

        flag = "  <<< EXPIRY TODAY" if expiry == today else ""
        step = min((round(b - a, 2) for a, b in zip(strikes, strikes[1:])),
                   default=0)
        print(f"\n[ATM ] {name}: spot {spot:,.2f} -> strike {atm:,.0f} "
              f"| expiry {expiry}{flag}")
        print(f"       {len(strikes)} strikes, step {step:g}")

        for side in ("CE", "PE"):
            row = chain[(chain["_strike"] == atm) & (chain["_opt"] == side)]
            if row.empty:
                print(f"       {side}: NOT FOUND")
                continue
            row = row.iloc[0]
            token = str(row[cols["token"]])
            trd = str(row[cols["trd"]])
            lot = row[cols["lot"]] if cols["lot"] else "?"
            print(f"       {side}: {trd:<28} token {token:<10} lot {lot}")

            with _lock:
                _ALIAS[token] = trd        # seed so updates key by symbol
            option_subs.append({
                "instrument_token": token,
                "exchange_segment": seg,
            })

    if not option_subs:
        print("\n[EXIT] nothing resolved")
        return

    total = len(spot_subs_idx) + len(spot_subs_scrip) + len(option_subs)
    if total > MAX_TOTAL_SCRIPS:
        raise ValueError(f"{total} subscriptions exceeds the {MAX_TOTAL_SCRIPS} limit")

    print(f"\n[SUB ] {len(option_subs)} option legs "
          f"({total}/{MAX_TOTAL_SCRIPS} total)")
    client.subscribe(instrument_tokens=option_subs)

    print("\n[RUN ] streaming... Ctrl-C to stop\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[RUN ] shutting down")
        try:
            client.un_subscribe(instrument_tokens=option_subs)
            if spot_subs_scrip:
                client.un_subscribe(instrument_tokens=spot_subs_scrip)
            if spot_subs_idx:
                client.un_subscribe(instrument_tokens=spot_subs_idx, isIndex=True)
            client.logout()
        except Exception as e:
            print(f"[RUN ] cleanup error: {e}")


if __name__ == "__main__":
    main()
