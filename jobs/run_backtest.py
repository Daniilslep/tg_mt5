# -*- coding: utf-8 -*-
import sys
from pathlib import Path


def _fix_stdio() -> None:
    """Windows Server часто в cp1252 — кириллица в print иначе падает."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_fix_stdio()
print("backtest: script started", flush=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

print("backtest: loading modules (pandas / MT5)...", flush=True)
from core.backtest import run_backtest  # noqa: E402

if __name__ == "__main__":
    print("backtest: run_backtest()", flush=True)
    run_backtest()
    print("backtest: done", flush=True)
