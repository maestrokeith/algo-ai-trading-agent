#!/usr/bin/env python3
"""Run read-only diagnostics against one news provider."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_app_config
from src.news_diagnostics import format_news_diagnostic, run_news_diagnostic


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only news provider diagnostics.")
    parser.add_argument("--provider", choices=("alpaca", "newsapi", "sec"), required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path, default=None, help="Config path; defaults to config/default.yaml.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config or args.project_root / "config" / "default.yaml"
    config = load_app_config(config_path)
    try:
        payload = run_news_diagnostic(
            provider=args.provider,
            symbol=args.symbol,
            hours=args.hours,
            limit=args.limit,
            config=config,
            project_root=args.project_root,
        )
    except Exception as exc:
        print(f"NEWS_DIAGNOSTICS_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(format_news_diagnostic(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
