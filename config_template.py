"""
Kotak Neo API credentials.

Fill these in, then keep this file out of git (see .gitignore).
"""

CONFIG = {
    # Neo app -> Invest -> TradeAPI -> API Dashboard -> Create Application
    "consumer_key": "YOUR_ACCESS_TOKEN",

    # Registered mobile number, with ISD code
    "mobile_number": "+91XXXXXXXXXX",

    # 5-character client code (Neo app -> Profile -> Client Code)
    "ucc": "ABC12",

    # 6-digit trading MPIN
    "mpin": "123456",

    # Setup key saved during TOTP registration (base32 seed).
    # With this, pyotp generates the 6-digit code automatically each login.
    "totp_secret": "YOUR_TOTP_SEED",

    # "prod" for live, "uat" for Kotak's test environment
    "environment": "prod",
}


# ---------------------------------------------------------------------------
# What to subscribe to
# ---------------------------------------------------------------------------

# Indices are subscribed by NAME (case-sensitive), with isIndex=True.
# LTP arrives in the 'iv' field.
INDICES = [
    {"instrument_token": "Nifty 50",   "exchange_segment": "nse_cm"},
    {"instrument_token": "Nifty Bank", "exchange_segment": "nse_cm"},
]

# Scrips / options are subscribed by numeric pSymbol from the scrip master CSV.
# LTP arrives in the 'ltp' field.
# Segments: nse_cm, bse_cm, nse_fo, bse_fo, cde_fo, mcx_fo
SCRIPS = [
    {"instrument_token": "11536", "exchange_segment": "nse_cm"},  # example: TCS-EQ
    # {"instrument_token": "<CE token>", "exchange_segment": "nse_fo"},
    # {"instrument_token": "<PE token>", "exchange_segment": "nse_fo"},
]
