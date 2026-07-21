"""
Runtime credential store
========================
Credentials are entered in the GUI and saved to `config.json` beside the EXE.
Nothing sensitive is compiled into the binary.

Load order:
    1. config.json  (written by the GUI)
    2. config.py    (legacy / console use)
    3. empty        -> GUI starts and asks for details
"""

import os
import sys
import json
import importlib.util

CONFIG_FILE = "config.json"

FIELDS = [
    ("consumer_key",  "Access Token (API Dashboard)", False),
    ("mobile_number", "Mobile Number (with +91)",     False),
    ("ucc",           "UCC / Client Code",            False),
    ("mpin",          "MPIN (6 digits)",              True),
    ("totp_secret",   "TOTP Secret (setup key)",      True),
]

DEFAULT = {
    "consumer_key": "", "mobile_number": "", "ucc": "",
    "mpin": "", "totp_secret": "", "environment": "prod",
}


def app_dir():
    """Folder containing the EXE (frozen) or this source file."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    return os.path.join(app_dir(), CONFIG_FILE)


def load_config():
    cfg = dict(DEFAULT)

    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                cfg.update(json.load(f))
            return cfg
        except Exception as e:
            print(f"[CONFIG] could not read {path}: {e}")

    legacy = os.path.join(app_dir(), "config.py")
    if os.path.exists(legacy):
        try:
            spec = importlib.util.spec_from_file_location("user_config", legacy)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cfg.update(getattr(mod, "CONFIG", {}))
        except Exception as e:
            print(f"[CONFIG] could not read config.py: {e}")

    return cfg


def save_config(values):
    """Persist credentials next to the EXE and update the live CONFIG."""
    cfg = dict(CONFIG)
    cfg.update(values)
    with open(config_path(), "w") as f:
        json.dump(cfg, f, indent=2)
    CONFIG.clear()
    CONFIG.update(cfg)          # same object, so login() sees new values
    return config_path()


def is_complete(cfg=None):
    cfg = cfg or CONFIG
    return all(str(cfg.get(k, "")).strip() for k, _, _ in FIELDS)


def missing_fields(cfg=None):
    cfg = cfg or CONFIG
    return [lbl for k, lbl, _ in FIELDS if not str(cfg.get(k, "")).strip()]


def ensure_file():
    """Create a blank config.json on first run so it is easy to find/edit."""
    path = config_path()
    if not os.path.exists(path):
        try:
            with open(path, "w") as f:
                json.dump(DEFAULT, f, indent=2)
        except Exception:
            pass
    return path


# Mutable module-level dict - the GUI updates this in place.
CONFIG = load_config()
ensure_file()
INDICES = []
SCRIPS = []
