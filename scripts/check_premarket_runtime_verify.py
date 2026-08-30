#!/usr/bin/env python3
"""Read-only live premarket runtime readiness verification."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.premarket_readiness import (
    check_premarket_readiness,
    format_premarket_runtime_symbol,
    format_premarket_runtime_verify,
    premarket_runtime_ready,
    premarket_runtime_symbol_rows,
    premarket_runtime_symbols,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="verify live catalyst runtime readiness")
    parser.add_argument("--verbose", action="store_true", help="print per-symbol catalyst metadata")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.live:
        print("PREMARKET_RUNTIME_VERIFY ready=false reason=live_flag_required rankings=0 catalysts=0 events=0 symbols=none")
        return 2
    project_root = args.project_root.resolve()
    readiness = check_premarket_readiness(project_root, now=datetime.now(timezone.utc))
    symbols = premarket_runtime_symbols(project_root)
    print(format_premarket_runtime_verify(readiness, symbols=symbols))
    if args.verbose:
        for row in premarket_runtime_symbol_rows(project_root, now=datetime.now(timezone.utc)):
            print(format_premarket_runtime_symbol(row))
    return 0 if premarket_runtime_ready(readiness) else 1


if __name__ == "__main__":
    sys.exit(main())
