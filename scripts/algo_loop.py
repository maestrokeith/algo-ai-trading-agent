#!/usr/bin/env python3
"""Alias for ``run_alpaca_loop.py`` (same CLI, including per-user file locks)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "_run_alpaca_loop",
    Path(__file__).resolve().parent / "run_alpaca_loop.py",
)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

if __name__ == "__main__":
    _mod.main()
