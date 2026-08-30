#!/usr/bin/env python3
"""Full regular-session replay using historical artifacts and a mock broker only."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import replay_live_cycle
from src.app.live_cycle import _market_session_entry_cadence_seconds
from src.config_loader import load_config
from src.loop_helpers import resolve_dynamic_momentum_intervals, resolve_live_loop_intervals
from src.report_dates import latest_market_session_replay_date

ET = ZoneInfo("America/New_York")


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True, default=str)
        fh.write("\n")


def _snapshot_time(path: Path) -> datetime | None:
    match = re.match(r"(\d{8})T(\d{6})(\d*)Z_", path.name)
    if not match:
        return None
    text = f"{match.group(1)}T{match.group(2)}"
    try:
        return datetime.strptime(text, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def find_session_history_snapshots(*, project_root: Path, user: str, day: str) -> list[Path]:
    """Return dynamic scan history snapshots for a user and market date."""
    history_dir = project_root / "data" / "dynamic_scan_history"
    if not history_dir.exists():
        return []
    token = day.replace("-", "")
    suffix = f"_{user}.json"
    rows = [
        path
        for path in history_dir.glob("*.json")
        if path.name.startswith(token) and path.name.endswith(suffix)
    ]
    return sorted(rows, key=lambda p: _snapshot_time(p) or datetime.min.replace(tzinfo=timezone.utc))


def build_session_ticks(
    *,
    day: str,
    config: Mapping[str, Any],
    max_ticks: int | None = None,
) -> list[datetime]:
    """Build simulated production-cadence ticks from 09:30 to 16:00 ET."""
    exit_min, entry_min = resolve_live_loop_intervals(config)
    dyn_entry_min, _dyn_exit_min = resolve_dynamic_momentum_intervals(config)
    default_dynamic_seconds = float(dyn_entry_min if dyn_entry_min is not None else entry_min) * 60.0
    default_core_seconds = float(entry_min) * 60.0
    cursor = datetime.fromisoformat(day).replace(tzinfo=ET, hour=9, minute=30, second=0, microsecond=0)
    close = cursor.replace(hour=16, minute=0)
    ticks: list[datetime] = []
    while cursor < close:
        ticks.append(cursor)
        dyn_sec, core_sec = _market_session_entry_cadence_seconds(
            cursor,
            default_dynamic_seconds=default_dynamic_seconds,
            default_core_seconds=default_core_seconds,
        )
        sleep_sec = max(60.0, min(float(exit_min) * 60.0, dyn_sec, core_sec))
        cursor += timedelta(seconds=sleep_sec)
        if max_ticks is not None and len(ticks) >= max_ticks:
            break
    return ticks


def _snapshot_for_tick(snapshots: list[Path], tick: datetime) -> Path | None:
    if not snapshots:
        return None
    tick_utc = tick.astimezone(timezone.utc)
    eligible = [path for path in snapshots if (_snapshot_time(path) or tick_utc) <= tick_utc]
    if eligible:
        return eligible[-1]
    return snapshots[0]


def _order_route(order: Mapping[str, Any], action_rows: list[Mapping[str, Any]]) -> str:
    symbol = str(order.get("symbol") or "").strip().upper()
    for row in action_rows:
        if str(row.get("symbol") or "").strip().upper() == symbol:
            route = str(row.get("route") or row.get("source") or "unknown").strip()
            return route or "unknown"
    return "unknown"


def _estimate_route_pnl(cycles: list[Mapping[str, Any]]) -> dict[str, float]:
    """Estimate route PnL from mock buy/sell notional flow when available."""
    pnl: defaultdict[str, float] = defaultdict(float)
    for cycle in cycles:
        action_rows = cycle.get("allocator_actions_created")
        actions = action_rows if isinstance(action_rows, list) else []
        orders = cycle.get("simulated_submitted_orders")
        for order in orders if isinstance(orders, list) else []:
            if not isinstance(order, Mapping):
                continue
            route = _order_route(order, actions)
            side = str(order.get("side") or "").lower()
            notional = replay_live_cycle._float(order, "notional", 0.0)
            if side == "sell":
                pnl[route] += notional
            elif side == "buy":
                pnl[route] += 0.0
    return {route: round(value, 6) for route, value in sorted(pnl.items())}


def _churn_stats(cycles: list[Mapping[str, Any]]) -> dict[str, Any]:
    symbol_sides: defaultdict[str, set[str]] = defaultdict(set)
    order_count: Counter[str] = Counter()
    buy_count: Counter[str] = Counter()
    sell_count: Counter[str] = Counter()
    for cycle in cycles:
        orders = cycle.get("simulated_submitted_orders")
        for order in orders if isinstance(orders, list) else []:
            if not isinstance(order, Mapping):
                continue
            symbol = str(order.get("symbol") or "").strip().upper()
            side = str(order.get("side") or "").strip().lower()
            if not symbol:
                continue
            order_count[symbol] += 1
            if side:
                symbol_sides[symbol].add(side)
            if side == "buy":
                buy_count[symbol] += 1
            elif side == "sell":
                sell_count[symbol] += 1
    reversal_symbols = sorted(sym for sym, sides in symbol_sides.items() if {"buy", "sell"}.issubset(sides))
    repeat_order_symbols = sorted(sym for sym, count in order_count.items() if count > 1)
    repeated_buy_symbols = sorted(sym for sym, count in buy_count.items() if count > 1)
    repeated_sell_symbols = sorted(sym for sym, count in sell_count.items() if count > 1)
    return {
        "same_day_reversal_count": len(reversal_symbols),
        "same_day_reversal_symbols": reversal_symbols,
        "repeat_order_count": len(repeat_order_symbols),
        "repeat_order_symbols": repeat_order_symbols,
        "repeated_buy_count": len(repeated_buy_symbols),
        "repeated_buy_symbols": repeated_buy_symbols,
        "repeated_sell_count": len(repeated_sell_symbols),
        "repeated_sell_symbols": repeated_sell_symbols,
    }


def _flatten(cycles: list[Mapping[str, Any]], key: str) -> list[Any]:
    out: list[Any] = []
    for cycle in cycles:
        rows = cycle.get(key)
        if isinstance(rows, list):
            out.extend(rows)
    return out


def _optional_artifact_listing(project_root: Path, relative_dir: str, *, limit: int = 50) -> list[str]:
    root = project_root / relative_dir
    if not root.exists():
        return []
    files = sorted(path for path in root.rglob("*.json") if path.is_file())
    return [str(path.relative_to(project_root)) for path in files[:limit]]


def _filtered_logs(cycles: list[Mapping[str, Any]], markers: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for cycle in cycles:
        lines = cycle.get("log_lines")
        for line in lines if isinstance(lines, list) else []:
            text = str(line)
            if any(marker in text for marker in markers):
                out.append(text)
    return out


def run_market_session_replay(
    *,
    project_root: Path = PROJECT_ROOT,
    day: str,
    user: str,
    broker_mock: bool,
    broker_mode: str | None = None,
    max_ticks: int | None = None,
    summary_dir: Path | None = None,
) -> dict[str, Any]:
    """Replay a full market session using historical snapshots and mock orders."""
    if str(day).strip().lower() == "latest":
        latest = latest_market_session_replay_date(project_root=project_root, user_id=user)
        if latest is None:
            raise RuntimeError(f"no_market_session_replay_date user={user}")
        day = latest
    mode = (broker_mode or os.environ.get("BROKER_MODE") or os.environ.get("ALPACA_BROKER_MODE") or "MOCK").strip().upper()
    if mode == "LIVE" and not broker_mock:
        raise RuntimeError("unsafe_live_broker_mode: market-session replay requires --broker-mock when broker mode is LIVE")
    if not broker_mock:
        raise RuntimeError("broker_mock_required: market-session replay never runs without --broker-mock")

    config = load_config(project_root / "config" / "default.yaml")
    ticks = build_session_ticks(day=day, config=config, max_ticks=max_ticks)
    session_start = datetime.fromisoformat(day).replace(tzinfo=ET, hour=9, minute=30, second=0, microsecond=0)
    session_end = session_start.replace(hour=16, minute=0)
    snapshots = find_session_history_snapshots(project_root=project_root, user=user, day=day)
    premarket = replay_live_cycle.load_premarket_artifacts(project_root)
    reports_dir = project_root / "data" / "reports"
    reports = sorted(str(p.relative_to(project_root)) for p in reports_dir.glob("*") if p.is_file()) if reports_dir.exists() else []
    cycles: list[dict[str, Any]] = []
    used_snapshots: list[str] = []
    intermediate_dir = (summary_dir or (project_root / "data" / "replay_market_session")) / "_cycles"

    for tick in ticks:
        snapshot = _snapshot_for_tick(snapshots, tick)
        if snapshot is None:
            continue
        used_snapshots.append(str(snapshot.relative_to(project_root)))
        cycle = replay_live_cycle.run_replay(
            project_root=project_root,
            date=day,
            user=user,
            broker_mock=True,
            broker_mode=mode,
            summary_dir=intermediate_dir,
            history_path_override=snapshot,
            now_override=tick.astimezone(timezone.utc),
        )
        cycle["tick_et"] = tick.isoformat()
        cycle["history_path"] = str(snapshot.relative_to(project_root))
        cycles.append(cycle)

    selected = _flatten(cycles, "selected_candidates")
    rejected_before = _flatten(cycles, "rejected_before_allocator")
    rejected_allocator = _flatten(cycles, "rejected_by_allocator")
    order_rejects = _flatten(cycles, "rejected_by_order_builder")
    mock_orders = _flatten(cycles, "simulated_submitted_orders")
    route_pnl = _estimate_route_pnl(cycles)
    summary = {
        "ok": True,
        "mode": "market_session_replay",
        "broker_mock": True,
        "broker_mode": mode,
        "user": user,
        "date": day,
        "clock": {
            "timezone": "America/New_York",
            "start": session_start.isoformat(),
            "end": session_end.isoformat(),
            "tick_count": len(ticks),
            "cycles_with_data": len(cycles),
        },
        "historical_artifacts": {
            "dynamic_scan_history": sorted(set(used_snapshots)),
            "bars": _optional_artifact_listing(project_root, "data/historical_bars"),
            "quotes": _optional_artifact_listing(project_root, "data/historical_quotes"),
            "premarket": sorted(premarket.keys()),
            "reports": reports[:20],
        },
        "mock_orders": mock_orders,
        "selected_candidates": selected,
        "rejected_candidates": rejected_before,
        "rejected_by_allocator": rejected_allocator,
        "rejected_by_order_builder": order_rejects,
        "route_level_pnl_estimate": route_pnl,
        "churn_same_day_reversal_stats": _churn_stats(cycles),
        "core_rebuild_logs": _filtered_logs(cycles, ("CORE_REBUILD",)),
        "dynamic_high_conviction_logs": _filtered_logs(cycles, ("HIGH_CONVICTION",)),
        "cycle_summaries": [
            {
                "tick_et": cycle.get("tick_et"),
                "history_path": cycle.get("history_path"),
                "mock_orders": cycle.get("simulated_submitted_orders", []),
                "selected_count": len(cycle.get("selected_candidates", [])),
                "rejected_count": len(cycle.get("rejected_candidates", [])),
            }
            for cycle in cycles
        ],
    }
    out_dir = summary_dir or (project_root / "data" / "replay_market_session")
    out_path = out_dir / f"{day}_{user}.json"
    summary["summary_path"] = str(out_path.relative_to(project_root) if out_path.is_relative_to(project_root) else out_path)
    _write_json(out_path, summary)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay a full market session from historical artifacts.")
    parser.add_argument("--date", required=True, help="Market date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="default")
    parser.add_argument("--broker-mock", action="store_true", help="Required. Force mock broker.")
    parser.add_argument("--broker-mode", default=None, help="Override broker mode for safety validation.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--max-ticks", type=int, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        summary = run_market_session_replay(
            project_root=args.project_root,
            day=args.date,
            user=args.user,
            broker_mock=bool(args.broker_mock),
            broker_mode=args.broker_mode,
            max_ticks=args.max_ticks,
        )
    except Exception as exc:
        print(f"MARKET_SESSION_REPLAY_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({k: summary[k] for k in ("ok", "user", "date", "summary_path")}, indent=2))
    print("session ticks:", summary["clock"]["tick_count"])
    print("cycles with data:", summary["clock"]["cycles_with_data"])
    print("mock orders:", len(summary["mock_orders"]))
    print("selected candidates:", len(summary["selected_candidates"]))
    print("rejected candidates:", len(summary["rejected_candidates"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
