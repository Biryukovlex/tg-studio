#!/usr/bin/env python3
"""CLI wrapper for the safe SQLite archive inventory."""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.migration.sqlite_inventory import main


if __name__ == "__main__":
    raise SystemExit(main())
