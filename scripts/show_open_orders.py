#!/usr/bin/env python3
"""Print open Alpaca orders without mutating broker state."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.brokers.alpaca_client import AlpacaBroker


MODE_CREDENTIALS = {
    "live": ("ALPACA_LIVE_API_KEY_ID", "ALPACA_LIVE_API_SECRET_KEY"),
    "paper": ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
}
MODE_DEFAULT_USERS = {"live": "live_bot", "paper": "paper_bot"}


def _get_field(order: Any, name: str, default: str = "") -> Any:
    if isinstance(order, dict):
        return order.get(name, default)
    return getattr(order, name, default)


def _field_text(order: Any, name: str) -> str:
    raw = _get_field(order, name, "")
    if raw is None:
        return ""
    return str(raw)


def format_open_orders(orders: Iterable[Any]) -> str:
    """Return a stable text table for raw Alpaca order objects or dict rows."""
    rows = ["symbol\tside\tqty\tstatus\tsubmitted_at"]
    for order in orders:
        submitted_at = (
            _get_field(order, "submitted_at", None)
            or _get_field(order, "created_at", None)
            or ""
        )
        rows.append(
            "\t".join(
                [
                    _field_text(order, "symbol"),
                    _field_text(order, "side"),
                    _field_text(order, "qty"),
                    _field_text(order, "status"),
                    str(submitted_at) if submitted_at is not None else "",
                ]
            )
        )
    return "\n".join(rows)


def credential_names_for_mode(mode: str) -> tuple[str, str]:
    return MODE_CREDENTIALS[str(mode).strip().lower()]


def default_user_for_mode(mode: str) -> str:
    return MODE_DEFAULT_USERS[str(mode).strip().lower()]


def missing_credentials_for_mode(mode: str, environ: dict[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [name for name in credential_names_for_mode(mode) if not str(env.get(name) or "").strip()]


def base_url_type_for_mode(mode: str) -> str:
    return "paper_trading" if mode == "paper" else "live_trading"


def fetch_open_orders(*, mode: str) -> list[Any]:
    """Fetch open orders through the current broker wrapper."""
    key_env, secret_env = credential_names_for_mode(mode)
    broker = AlpacaBroker(
        api_key=os.environ.get(key_env),
        secret=os.environ.get(secret_env),
        paper=(mode == "paper"),
    )
    return list(broker.list_orders(status="open") or [])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show open Alpaca orders.")
    parser.add_argument("--mode", choices=("live", "paper"), default=None, help="Alpaca account mode. Default: live.")
    parser.add_argument("--user", default=None, help="Operator user label. Defaults to live_bot for live, paper_bot for paper.")
    compat = parser.add_mutually_exclusive_group()
    compat.add_argument("--paper", action="store_true", help=argparse.SUPPRESS)
    compat.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    mode = args.mode or ("paper" if args.paper else "live")
    if args.live:
        mode = "live"
    user = args.user or default_user_for_mode(mode)
    print(f"open_orders_context mode={mode} user={user} base_url_type={base_url_type_for_mode(mode)}")
    missing = missing_credentials_for_mode(mode)
    if missing:
        print(
            "open_orders_unavailable: missing credentials for "
            f"{mode} mode: {', '.join(missing)}",
            file=sys.stderr,
        )
        print(format_open_orders([]))
        return 0
    try:
        orders = fetch_open_orders(mode=mode)
    except ValueError as exc:
        print(f"open_orders_unavailable: {exc}", file=sys.stderr)
        print(format_open_orders([]))
        return 0
    print(format_open_orders(orders))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
