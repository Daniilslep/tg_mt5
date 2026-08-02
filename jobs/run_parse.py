# -*- coding: utf-8 -*-
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.parser import run_parse  # noqa: E402

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else None
    run_parse(mode)
