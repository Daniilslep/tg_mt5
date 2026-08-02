# -*- coding: utf-8 -*-
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.backtest import run_backtest  # noqa: E402

if __name__ == "__main__":
    run_backtest()
