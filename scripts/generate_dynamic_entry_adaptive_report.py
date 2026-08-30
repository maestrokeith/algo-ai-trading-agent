#!/usr/bin/env python3
"""Generate read-only dynamic-entry adaptive sensitivity report."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_config  # noqa: E402
from src.dynamic_entry_adaptive import (  # noqa: E402
    render_dynamic_entry_adaptive_report,
    write_dynamic_entry_adaptive_report,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.project_root / "config" / "default.yaml")
    json_path, text_path, report = write_dynamic_entry_adaptive_report(
        data_dir=args.data_dir,
        user_id=args.user,
        report_date=args.date,
        config=config,
        context={"market_regime": "normal"},
    )
    print(render_dynamic_entry_adaptive_report(report))
    print(f"JSON: {json_path}")
    print(f"Markdown: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
