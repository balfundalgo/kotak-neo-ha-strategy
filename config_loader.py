"""
Runtime config loader
=====================
Keeps credentials OUT of the EXE.

Running from source  -> imports config.py normally.
Running as an EXE    -> loads config.py from the folder containing the EXE.

So each client gets: KotakHAStrategy.exe + their own config.py alongside it.
Nothing sensitive is compiled into the binary.
"""

import os
import sys
import importlib.util


def _config_dir():
    if getattr(sys, "frozen", False):          # PyInstaller bundle
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config():
    path = os.path.join(_config_dir(), "config.py")

    if not os.path.exists(path):
        print("=" * 70)
        print(" config.py NOT FOUND")
        print("=" * 70)
        print(f" Expected at: {path}")
        print()
        print(" Create a config.py in this folder containing CONFIG,")
        print(" INDICES and SCRIPS. See config_template.py.")
        print("=" * 70)
        input(" Press Enter to exit...")
        sys.exit(1)

    spec = importlib.util.spec_from_file_location("user_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    missing = [k for k in ("CONFIG",) if not hasattr(mod, k)]
    if missing:
        print(f"config.py is missing: {', '.join(missing)}")
        input("Press Enter to exit...")
        sys.exit(1)

    return (
        mod.CONFIG,
        getattr(mod, "INDICES", []),
        getattr(mod, "SCRIPS", []),
    )


CONFIG, INDICES, SCRIPS = load_config()
