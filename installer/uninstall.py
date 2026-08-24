#!/usr/bin/env python3
import importlib
import sys
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1]
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

main = importlib.import_module("kikit_packer.uninstall").main


if __name__ == "__main__":
    raise SystemExit(main())
