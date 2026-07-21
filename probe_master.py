"""
Scrip-master probe
==================
Answers two open questions before we trust the resolver:

  1. What is SENSEX actually called in pSymbolName? (SENSEX vs BSXOPT vs ...)
  2. Does MCX list OPTIONS on CRUDEOILM, or only on full-size CRUDEOIL?

Run:  python probe_master.py
"""

from datetime import datetime, timezone, timedelta
import pandas as pd

from kotak_ws_base import login
from option_chain import load_scrip_master, to_expiry_date

IST = timezone(timedelta(hours=5, minutes=30))
TODAY = datetime.now(IST).date()

SEGMENTS = {
    "nse_fo": ["NIFTY"],
    "bse_fo": ["SENSEX", "SENSEX50", "BANKEX"],
    "mcx_fo": ["CRUDEOILM", "CRUDEOIL"],
}


def main():
    client = login()

    for seg, wanted in SEGMENTS.items():
        df = load_scrip_master(client, seg)
        print(f"\n{'=' * 70}\n{seg}  ({len(df):,} rows)\n{'=' * 70}")

        # instrument types present
        print("pInstType values:", sorted(df["pInstType"].dropna().astype(str).unique()))

        # option rows are those carrying CE/PE
        is_opt = df["pOptionType"].astype(str).str.upper().str.strip().isin(["CE", "PE"])
        print(f"option rows: {is_opt.sum():,}")

        # which underlyings actually have options
        opt_names = (df.loc[is_opt, "pSymbolName"].astype(str).str.upper()
                     .value_counts())
        print("\ntop underlyings WITH options:")
        print(opt_names.head(15).to_string())

        # targeted look-ups
        for name in wanted:
            sub = df[df["pSymbolName"].astype(str).str.upper().str.strip() == name]
            n_opt = sub["pOptionType"].astype(str).str.upper().isin(["CE", "PE"]).sum()
            n_fut = len(sub) - n_opt
            print(f"\n--- {name}: {len(sub):,} rows | options {n_opt:,} | non-options {n_fut:,}")
            if sub.empty:
                # fuzzy: anything starting with the first 5 chars
                like = (df[df["pSymbolName"].astype(str).str.upper()
                        .str.startswith(name[:5])]["pSymbolName"]
                        .astype(str).str.upper().unique())
                print(f"    not found. similar names: {sorted(like)[:20]}")
                continue

            print(f"    pInstType: {sorted(sub['pInstType'].dropna().astype(str).unique())}")

            if n_opt:
                o = sub[sub["pOptionType"].astype(str).str.upper().isin(["CE", "PE"])].copy()
                o["_exp"] = o["lExpiryDate"].apply(lambda e: to_expiry_date(e, seg))
                exps = sorted(e for e in o["_exp"].unique() if e >= TODAY)[:5]
                print(f"    next expiries: {exps}")
                if exps:
                    near = o[o["_exp"] == exps[0]]
                    prec = pd.to_numeric(near["lPrecision"], errors="coerce").fillna(2)
                    strikes = sorted(
                        (pd.to_numeric(near["dStrikePrice"], errors="coerce")
                         / (10 ** prec)).dropna().unique())
                    print(f"    strikes on {exps[0]}: {len(strikes)} "
                          f"[{strikes[0]:,.0f} .. {strikes[-1]:,.0f}]")
                    if len(strikes) > 1:
                        steps = {round(b - a, 2) for a, b in zip(strikes, strikes[1:])}
                        print(f"    strike steps seen: {sorted(steps)[:5]}")
                    print(f"    sample: {near['pTrdSymbol'].head(3).tolist()}")
                    print(f"    lot size: {sorted(near['lLotSize'].dropna().unique())[:5]}")

            if n_fut:
                f = sub[~sub["pOptionType"].astype(str).str.upper().isin(["CE", "PE"])].copy()
                f["_exp"] = f["lExpiryDate"].apply(lambda e: to_expiry_date(e, seg))
                fe = sorted(e for e in f["_exp"].unique() if e >= TODAY)[:4]
                print(f"    futures expiries: {fe}")
                print(f"    futures sample: {f['pTrdSymbol'].head(3).tolist()}")


if __name__ == "__main__":
    main()
