#!/usr/bin/env python3
"""Generate a read-only June session candidate-to-order diagnostic report."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.generate_dynamic_gate_research import main


if __name__ == "__main__":
    raise SystemExit(main())
