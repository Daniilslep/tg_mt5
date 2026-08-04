# -*- coding: utf-8 -*-
import sys
from pathlib import Path


def _fix_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_fix_stdio()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.parser import run_parse  # noqa: E402

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    run_parse(mode)
