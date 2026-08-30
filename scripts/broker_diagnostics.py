#!/usr/bin/env python3
"""Read-only broker diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.brokers.broker_factory import broker_provider, get_broker
from src.config_loader import load_config
from src.trading_control import resolve_trading_mode

MUTATING_BROKER_METHODS = {
    "submit_order",
    "cancel_order",
    "cancel_order_by_id",
    "submit_option_order",
    "cancel_option_order",
    "review_equity_order",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "__dict__"):
        return {k: _jsonable(v) for k, v in value.__dict__.items() if not k.startswith("_")}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _date_key(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 else None


def _order_matches_date(order: Any, day: str | None) -> bool:
    if not day:
        return True
    fields = (
        getattr(order, "submitted_at", None),
        getattr(order, "updated_at", None),
        getattr(order, "filled_at", None),
    )
    raw = getattr(order, "raw", None)
    if isinstance(raw, Mapping):
        fields = fields + (raw.get("created_at"), raw.get("submitted_at"), raw.get("updated_at"), raw.get("filled_at"))
    if isinstance(order, Mapping):
        fields = fields + (order.get("created_at"), order.get("submitted_at"), order.get("updated_at"), order.get("filled_at"))
    return str(day)[:10] in {_date_key(value) for value in fields}


def _read_only_invariant(_broker: Any) -> None:
    # Diagnostics must never call these methods. This function centralizes the
    # invariant next to the command implementation so future commands have a
    # visible denylist to check against.
    return None


def _config_with_provider(config: dict[str, Any], provider: str | None) -> dict[str, Any]:
    if not provider:
        return config
    out = dict(config)
    broker = dict(_mapping(out.get("broker")))
    broker["provider"] = provider
    broker["firm"] = provider
    out["broker"] = broker
    return out


def build_status(
    config: dict[str, Any],
    *,
    provider: str | None = None,
    include_positions: bool = False,
    include_orders: bool = False,
    order_date: str | None = None,
) -> dict[str, Any]:
    cfg = _config_with_provider(config, provider)
    selected = broker_provider(cfg)
    report: dict[str, Any] = {
        "provider": selected,
        "authenticated": False,
        "account_type": None,
        "trading_enabled": False,
        "options_enabled": False,
        "buying_power": None,
        "positions": [],
        "open_orders": [],
        "capabilities": {},
        "errors": [],
    }
    try:
        mode = resolve_trading_mode(cfg, paper=bool(_mapping(cfg.get("broker")).get("paper", True)), live_operation=False)
        report["trading_mode"] = mode.mode
        report["trading_enabled"] = bool(mode.broker_orders_allowed)
    except Exception as exc:
        report["trading_mode"] = "invalid"
        report["errors"].append(f"trading_mode:{type(exc).__name__}")

    try:
        broker = get_broker(cfg)
        _read_only_invariant(broker)
        report["capabilities"] = _jsonable(getattr(broker, "capabilities", {}))
        account = broker.get_account()
        report["authenticated"] = True
        report["account_type"] = getattr(account, "account_type", None)
        report["buying_power"] = getattr(account, "buying_power", None)
        report["equity"] = getattr(account, "equity", None)
        if include_positions:
            positions = broker.list_positions() if hasattr(broker, "list_positions") else broker.get_positions()
            report["positions"] = _jsonable(positions)
        if include_orders:
            if order_date:
                try:
                    orders = broker.list_orders(status="all", date=order_date)
                except TypeError:
                    orders = [order for order in broker.list_orders(status="all") if _order_matches_date(order, order_date)]
            else:
                orders = broker.list_orders(status="open")
            report["open_orders"] = _jsonable([order for order in orders if _order_matches_date(order, order_date)])
            report["order_date"] = order_date
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}:{exc}")
    return report


def _print(report: Mapping[str, Any], *, as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if not report.get("errors") else 2
    print(f"provider: {report.get('provider')}")
    print(f"authenticated: {str(bool(report.get('authenticated'))).lower()}")
    print(f"account type: {report.get('account_type') or 'unknown'}")
    print(f"trading enabled: {str(bool(report.get('trading_enabled'))).lower()}")
    print(f"options enabled: {str(bool(report.get('options_enabled'))).lower()}")
    print(f"buying power: {report.get('buying_power')}")
    if "execution_enabled" in report:
        print(f"execution flag: {str(bool(report.get('execution_enabled'))).lower()}")
    if report.get("capabilities"):
        print("capabilities:")
        for key, value in report["capabilities"].items():
            print(f"  {key}: {str(bool(value)).lower()}")
    if report.get("positions"):
        print(f"positions: {len(report['positions'])}")
    if report.get("open_orders"):
        print(f"open orders: {len(report['open_orders'])}")
    for err in report.get("errors") or []:
        print(f"error: {err}")
    return 0 if not report.get("errors") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only broker diagnostics")
    parser.add_argument("command", choices=["status", "capabilities", "positions", "orders"])
    parser.add_argument("--broker", choices=["alpaca"], default=None)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "default.yaml")
    parser.add_argument("--date", default=None, help="Filter broker order history by YYYY-MM-DD.")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    provider = args.broker
    include_positions = args.command == "positions"
    include_orders = args.command == "orders"
    if args.date and args.command != "orders":
        parser.error("--date is only supported with broker-orders")
    report = build_status(config, provider=provider, include_positions=include_positions, include_orders=include_orders, order_date=args.date)
    return _print(report, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
