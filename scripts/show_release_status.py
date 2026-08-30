#!/usr/bin/env python3
"""Show the current read-only release identity."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.release_status import collect_release_status, format_release_status


def main() -> int:
    print(format_release_status(collect_release_status(PROJECT_ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
