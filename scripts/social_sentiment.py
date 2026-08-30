#!/usr/bin/env python3
"""Run read-only social sentiment diagnostics."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config_loader import load_app_config
from src.social_sentiment import collect_social_sentiment, format_social_sentiment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only social sentiment diagnostics.")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols, e.g. AAPL,NVDA,PLTR.")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--config", type=Path, default=None, help="Config path; defaults to config/default.yaml.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    symbols = [item.strip() for item in str(args.symbols or "").split(",") if item.strip()]
    config_path = args.config or args.project_root / "config" / "default.yaml"
    config = load_app_config(config_path)
    try:
        payload = collect_social_sentiment(
            symbols=symbols,
            config=config,
            project_root=args.project_root,
            hours=args.hours,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"SOCIAL_SENTIMENT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(format_social_sentiment(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
