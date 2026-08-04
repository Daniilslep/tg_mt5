# -*- coding: utf-8 -*-
import sys
from pathlib import Path

print("backtest: скрипт запущен", flush=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

print("backtest: загружаю модули (pandas / MT5)…", flush=True)
from core.backtest import run_backtest  # noqa: E402

if __name__ == "__main__":
    print("backtest: старт run_backtest()", flush=True)
    run_backtest()
    print("backtest: конец", flush=True)
