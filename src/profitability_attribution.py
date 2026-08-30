"""Daily profitability attribution reporting.

This module is intentionally read-only with respect to trading state. It
combines existing attribution, order history, and daily summary artifacts into
a daily PnL report that can be rendered in the CLI and saved as JSON.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.trade_attribution import attribution_daily_path, load_daily_artifact

ROUTE_BUCKETS: tuple[str, ...] = (
    "core_rebuild",
    "dynamic_momentum",
    "trend_long",
    "options_paper",
    "allocator_rotation",
    "unknown",
)
WEAK_EXIT_MAX_HOLD_MINUTES = 30.0


@dataclass(frozen=True)
class ProfitabilityTrade:
    """Normalized realized-PnL row used by the profitability report."""

    symbol: str
    route: str
    pnl: float
    pnl_pct: float | None = None
    source: str | None = None
    timestamp: str | None = None
    reason: str | None = None


def profitability_daily_path(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str | None = None,
) -> Path:
    """Return the daily profitability attribution artifact path."""
    day_s = day.isoformat() if isinstance(day, date) else str(day or date.today().isoformat())
    user_s = str(user_id or "default").strip() or "default"
    return Path(data_dir) / "profitability_attribution" / "daily" / f"{day_s}_{user_s}.json"


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _load_json(path: Path | str | None) -> Any | None:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _first_float(mapping: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        if "." in key:
            cur: Any = mapping
            for part in key.split("."):
                if not isinstance(cur, Mapping):
                    cur = None
                    break
                cur = cur.get(part)
            value = cur
        else:
            value = mapping.get(key)
        out = _safe_float(value)
        if out is not None:
            return out
    return None


def normalize_route(*values: Any) -> str:
    """Map raw route/source labels into stable profitability buckets."""
    text = " ".join(str(v or "") for v in values).strip().lower()
    if not text:
        return "unknown"
    compact = text.replace("-", "_").replace(" ", "_")
    if "core_rebuild" in compact:
        return "core_rebuild"
    if "option" in compact and ("paper" in compact or "shadow" in compact):
        return "options_paper"
    if "allocator_rotation" in compact or compact in {"rotation", "rebalance"}:
        return "allocator_rotation"
    if "dynamic" in compact or "momentum" in compact or "news_catalyst" in compact:
        return "dynamic_momentum"
    if "trend_long" in compact or compact in {"trend", "manual_or_core", "core"}:
        return "trend_long"
    return "unknown"


def _iter_rows(payload: Any, candidate_keys: Sequence[str]) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in candidate_keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _row_symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("sym_u") or "UNKNOWN").strip().upper() or "UNKNOWN"


def _trades_from_attribution(payload: Mapping[str, Any] | None) -> list[ProfitabilityTrade]:
    exits = _iter_rows(payload or {}, ("exits",))
    out: list[ProfitabilityTrade] = []
    for row in exits:
        pnl = _safe_float(row.get("pnl"))
        if pnl is None:
            continue
        route = normalize_route(row.get("entry_route"), row.get("entry_source"), row.get("route"), row.get("source"))
        out.append(
            ProfitabilityTrade(
                symbol=str(row.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN",
                route=route,
                pnl=pnl,
                pnl_pct=_safe_float(row.get("pnl_pct")),
                source=str(row.get("entry_source") or row.get("source") or "") or None,
                timestamp=str(row.get("timestamp") or "") or None,
                reason=str(row.get("exit_reason") or "") or None,
            )
        )
    return out


def _trades_from_order_history(payload: Any) -> list[ProfitabilityTrade]:
    rows = _iter_rows(payload, ("orders", "trades", "filled_orders", "order_history"))
    out: list[ProfitabilityTrade] = []
    for row in rows:
        pnl = _first_float(
            row,
            (
                "pnl",
                "realized_pnl",
                "profit_loss",
                "realized_profit_loss",
                "realized_pl",
            ),
        )
        if pnl is None:
            continue
        route = normalize_route(
            row.get("route"),
            row.get("strategy"),
            row.get("source"),
            row.get("entry_route"),
            row.get("entry_source"),
            row.get("client_order_id"),
        )
        out.append(
            ProfitabilityTrade(
                symbol=str(row.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN",
                route=route,
                pnl=pnl,
                pnl_pct=_first_float(row, ("pnl_pct", "return_pct", "realized_return_pct", "profit_loss_pct")),
                source=str(row.get("source") or row.get("strategy") or "") or None,
                timestamp=str(row.get("filled_at") or row.get("timestamp") or row.get("submitted_at") or "") or None,
                reason=str(row.get("exit_reason") or row.get("reason") or "") or None,
            )
        )
    return out


def _daily_summary_pnl(payload: Mapping[str, Any] | None) -> tuple[float | None, float | None, float | None]:
    if not isinstance(payload, Mapping):
        return None, None, None
    realized = _first_float(
        payload,
        (
            "realized_pnl",
            "realized",
            "daily_realized",
            "pnl.realized",
            "account.realized_pnl",
        ),
    )
    unrealized = _first_float(
        payload,
        (
            "unrealized_pnl",
            "unrealized",
            "daily_unrealized",
            "pnl.unrealized",
            "account.unrealized_pnl",
        ),
    )
    if unrealized is None:
        positions = payload.get("positions")
        if isinstance(positions, list):
            values = [
                _first_float(row, ("unrealized_pnl", "unrealized_pl", "pnl"))
                for row in positions
                if isinstance(row, Mapping)
            ]
            finite = [v for v in values if v is not None]
            if finite:
                unrealized = sum(finite)
    total = _first_float(
        payload,
        (
            "total_pnl",
            "total",
            "pnl.total",
            "pnl_today",
            "account.pnl_today",
        ),
    )
    return realized, unrealized, total


def _route_stats(trades: Sequence[ProfitabilityTrade]) -> dict[str, dict[str, Any]]:
    by_route: dict[str, list[ProfitabilityTrade]] = {route: [] for route in ROUTE_BUCKETS}
    for trade in trades:
        by_route.setdefault(trade.route, []).append(trade)
    stats: dict[str, dict[str, Any]] = {}
    for route in ROUTE_BUCKETS:
        rows = by_route.get(route, [])
        wins = [row.pnl for row in rows if row.pnl > 0.0]
        losses = [row.pnl for row in rows if row.pnl < 0.0]
        gross_gain = sum(wins)
        gross_loss = abs(sum(losses))
        if gross_loss > 0.0:
            profit_factor: float | None = gross_gain / gross_loss
        elif gross_gain > 0.0:
            profit_factor = None
        else:
            profit_factor = 0.0
        stats[route] = {
            "trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(rows)) if rows else 0.0,
            "avg_gain": (gross_gain / len(wins)) if wins else 0.0,
            "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
            "profit_factor": profit_factor,
        }
    return stats


def _trade_dict(trade: ProfitabilityTrade) -> dict[str, Any]:
    return {
        "symbol": trade.symbol,
        "route": trade.route,
        "pnl": round(trade.pnl, 6),
        "pnl_pct": None if trade.pnl_pct is None else round(trade.pnl_pct, 6),
        "source": trade.source,
        "timestamp": trade.timestamp,
        "reason": trade.reason,
    }


def _exit_reason_stats(trades: Sequence[ProfitabilityTrade]) -> dict[str, dict[str, Any]]:
    by_reason: defaultdict[str, list[ProfitabilityTrade]] = defaultdict(list)
    for trade in trades:
        reason = str(trade.reason or "unknown").strip() or "unknown"
        by_reason[reason].append(trade)
    return {
        reason: {
            "exits": len(rows),
            "pnl": round(sum(row.pnl for row in rows), 6),
            "wins": len([row for row in rows if row.pnl > 0.0]),
            "losses": len([row for row in rows if row.pnl < 0.0]),
        }
        for reason, rows in sorted(by_reason.items())
    }


def build_profitability_report(
    *,
    user_id: str,
    day: date | str,
    attribution_payload: Mapping[str, Any] | None = None,
    order_history_payload: Any | None = None,
    daily_summary_payload: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Build a daily profitability attribution report from local artifacts."""
    trades = _trades_from_attribution(attribution_payload)
    trade_source = "trade_attribution"
    if not trades:
        trades = _trades_from_order_history(order_history_payload)
        trade_source = "order_history" if trades else "none"

    summary_realized, summary_unrealized, summary_total = _daily_summary_pnl(daily_summary_payload)
    trade_realized = sum(row.pnl for row in trades)
    realized = trade_realized if trades else (summary_realized or 0.0)
    unrealized = summary_unrealized or 0.0
    total = realized + unrealized
    if not trades and summary_total is not None and summary_unrealized is None:
        total = summary_total

    pnl_by_route: dict[str, float] = {route: 0.0 for route in ROUTE_BUCKETS}
    by_route_accum: defaultdict[str, float] = defaultdict(float)
    for trade in trades:
        by_route_accum[trade.route] += trade.pnl
    for route in ROUTE_BUCKETS:
        pnl_by_route[route] = round(by_route_accum.get(route, 0.0), 6)

    winners = sorted((trade for trade in trades if trade.pnl > 0.0), key=lambda t: t.pnl, reverse=True)
    losers = sorted((trade for trade in trades if trade.pnl < 0.0), key=lambda t: t.pnl)
    gen = generated_at or datetime.now(timezone.utc)
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    return {
        "version": 1,
        "date": day_s,
        "user_id": str(user_id or "default"),
        "generated_at": gen.isoformat(),
        "inputs": {
            "trade_attribution_available": bool(attribution_payload),
            "order_history_available": order_history_payload is not None,
            "daily_summary_available": daily_summary_payload is not None,
            "realized_trade_source": trade_source,
        },
        "overall_pnl": {
            "realized": round(realized, 6),
            "unrealized": round(unrealized, 6),
            "total": round(total, 6),
        },
        "pnl_by_route": pnl_by_route,
        "route_stats": _route_stats(trades),
        "exit_reason_stats": _exit_reason_stats(trades),
        "top_winners": [_trade_dict(row) for row in winners[:10]],
        "top_losers": [_trade_dict(row) for row in losers[:10]],
    }


def _submitted_order_rows(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    rows = _iter_rows(payload or {}, ("orders",))
    return [
        row
        for row in rows
        if row.get("submitted") is True
        or str(row.get("order_build_status") or "").strip().lower() == "built"
    ]


def _replay_order_rows(payload: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return _iter_rows(payload or {}, ("mock_orders", "simulated_submitted_orders"))


def _order_side(row: Mapping[str, Any]) -> str:
    return str(row.get("side") or row.get("action") or "").strip().lower()


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in sorted(counter.items())}


def _weak_exit(row: Mapping[str, Any]) -> bool:
    pnl = _safe_float(row.get("pnl"))
    pnl_pct = _safe_float(row.get("pnl_pct"))
    hold = _safe_float(row.get("hold_minutes"))
    reason = str(row.get("exit_reason") or row.get("reason") or "").strip().lower()
    return (
        (hold is not None and hold <= WEAK_EXIT_MAX_HOLD_MINUTES)
        or (pnl is not None and pnl < 0.0)
        or (pnl_pct is not None and pnl_pct < 0.0)
        or reason in {"stop_loss", "signal_flip", "momentum_fade", "news_sentiment"}
    )


def build_trade_churn_analysis(
    *,
    user_id: str,
    day: date | str,
    attribution_payload: Mapping[str, Any] | None = None,
    replay_payload: Mapping[str, Any] | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Summarize churn and same-day reversal signals from read-only artifacts."""
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    attribution_orders = _submitted_order_rows(attribution_payload)
    replay_orders = _replay_order_rows(replay_payload)
    exits = _iter_rows(attribution_payload or {}, ("exits",))
    side_by_symbol: defaultdict[str, set[str]] = defaultdict(set)
    same_side_counts: dict[str, Counter[str]] = {"buy": Counter(), "sell": Counter(), "exit": Counter()}
    events: list[dict[str, Any]] = []

    for source, rows in (("trade_attribution", attribution_orders), ("replay", replay_orders)):
        for row in rows:
            symbol = _row_symbol(row)
            side = _order_side(row)
            if side not in {"buy", "sell"}:
                continue
            side_by_symbol[symbol].add(side)
            same_side_counts[side][symbol] += 1
            events.append({"source": source, "symbol": symbol, "side": side})

    for row in exits:
        symbol = _row_symbol(row)
        side_by_symbol[symbol].add("sell")
        same_side_counts["exit"][symbol] += 1
        events.append({"source": "trade_attribution", "symbol": symbol, "side": "exit"})

    repeated_buys = sorted(sym for sym, count in same_side_counts["buy"].items() if count > 1)
    repeated_sells = sorted(
        sym
        for sym in set(same_side_counts["sell"]) | set(same_side_counts["exit"])
        if same_side_counts["sell"].get(sym, 0) + same_side_counts["exit"].get(sym, 0) > 1
    )
    reversal_symbols = sorted(sym for sym, sides in side_by_symbol.items() if {"buy", "sell"}.issubset(sides))
    weak_exit_rows = [row for row in exits if _weak_exit(row)]
    weak_by_reason = Counter(str(row.get("exit_reason") or "unknown") for row in weak_exit_rows)
    weak_by_route = Counter(
        normalize_route(row.get("entry_route"), row.get("entry_source"), row.get("route"), row.get("source"))
        for row in weak_exit_rows
    )

    return {
        "version": 1,
        "date": day_s,
        "user_id": str(user_id or "default"),
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "inputs": {
            "trade_attribution_available": bool(attribution_payload),
            "replay_available": bool(replay_payload),
        },
        "order_activity": {
            "submitted_order_count": len(attribution_orders) + len(replay_orders),
            "buy_counts_by_symbol": _counter_dict(same_side_counts["buy"]),
            "sell_counts_by_symbol": _counter_dict(same_side_counts["sell"]),
            "exit_counts_by_symbol": _counter_dict(same_side_counts["exit"]),
        },
        "same_day_reversals": {
            "count": len(reversal_symbols),
            "symbols": reversal_symbols,
        },
        "repeated_activity": {
            "repeated_buy_count": len(repeated_buys),
            "repeated_buy_symbols": repeated_buys,
            "repeated_sell_count": len(repeated_sells),
            "repeated_sell_symbols": repeated_sells,
        },
        "weak_exits": {
            "count": len(weak_exit_rows),
            "max_hold_minutes": WEAK_EXIT_MAX_HOLD_MINUTES,
            "by_reason": _counter_dict(weak_by_reason),
            "by_route": _counter_dict(weak_by_route),
            "rows": [
                {
                    "symbol": _row_symbol(row),
                    "route": normalize_route(
                        row.get("entry_route"),
                        row.get("entry_source"),
                        row.get("route"),
                        row.get("source"),
                    ),
                    "reason": row.get("exit_reason") or row.get("reason"),
                    "pnl": _safe_float(row.get("pnl")),
                    "pnl_pct": _safe_float(row.get("pnl_pct")),
                    "hold_minutes": _safe_float(row.get("hold_minutes")),
                    "timestamp": row.get("timestamp"),
                }
                for row in weak_exit_rows
            ],
        },
        "events": events,
    }


def discover_order_history_path(*, data_dir: Path | str, user_id: str, day: date | str) -> Path | None:
    """Return the first known local order-history artifact path, if present."""
    d = Path(data_dir)
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    user_s = str(user_id or "default").strip() or "default"
    candidates = (
        d / "order_history" / f"{day_s}_{user_s}.json",
        d / "orders" / f"{day_s}_{user_s}.json",
        d / "reports" / f"{day_s}_{user_s}.json",
        d / "reports" / f"daily_{day_s}_{user_s}.json",
        d / "replay" / f"{day_s}_{user_s}.json",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def discover_daily_summary_path(*, data_dir: Path | str, user_id: str, day: date | str) -> Path | None:
    """Return the first known local daily-summary artifact path, if present."""
    d = Path(data_dir)
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    user_s = str(user_id or "default").strip() or "default"
    candidates = (
        d / "daily_summary" / f"{day_s}_{user_s}.json",
        d / "reports" / f"daily_summary_{day_s}_{user_s}.json",
        d / "reports" / f"{day_s}_{user_s}.json",
        d / "replay" / f"{day_s}_{user_s}.json",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def discover_replay_summary_path(*, data_dir: Path | str, user_id: str, day: date | str) -> Path | None:
    """Return the first known replay summary path, if present."""
    d = Path(data_dir)
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    user_s = str(user_id or "default").strip() or "default"
    candidates = (
        d / "replay_market_session" / f"{day_s}_{user_s}.json",
        d / "replay" / f"{day_s}_{user_s}.json",
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def load_profitability_report_inputs(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    order_history_path: Path | str | None = None,
    daily_summary_path: Path | str | None = None,
) -> tuple[Mapping[str, Any] | None, Any | None, Mapping[str, Any] | None]:
    """Load local artifacts used by the profitability report."""
    attr_path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    attr = load_daily_artifact(attr_path) if attr_path.exists() else None
    order_path = Path(order_history_path) if order_history_path else discover_order_history_path(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
    )
    summary_path = Path(daily_summary_path) if daily_summary_path else discover_daily_summary_path(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
    )
    order_payload = _load_json(order_path)
    summary_payload = _load_json(summary_path)
    return (
        attr if isinstance(attr, Mapping) else None,
        order_payload,
        summary_payload if isinstance(summary_payload, Mapping) else None,
    )


def load_trade_churn_analysis(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    replay_summary_path: Path | str | None = None,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Load local artifacts and return same-day churn analysis."""
    attr_path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    attr = load_daily_artifact(attr_path) if attr_path.exists() else None
    replay_path = Path(replay_summary_path) if replay_summary_path else discover_replay_summary_path(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
    )
    replay = _load_json(replay_path)
    return build_trade_churn_analysis(
        user_id=user_id,
        day=day,
        attribution_payload=attr if isinstance(attr, Mapping) else None,
        replay_payload=replay if isinstance(replay, Mapping) else None,
        generated_at=generated_at,
    )


def write_profitability_report(
    report: Mapping[str, Any],
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> Path:
    """Persist a daily profitability attribution report as JSON."""
    path = profitability_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def format_profitability_report(report: Mapping[str, Any]) -> str:
    """Render a compact CLI profitability report."""
    overall = report.get("overall_pnl") if isinstance(report.get("overall_pnl"), Mapping) else {}
    pnl_by_route = report.get("pnl_by_route") if isinstance(report.get("pnl_by_route"), Mapping) else {}
    route_stats = report.get("route_stats") if isinstance(report.get("route_stats"), Mapping) else {}
    lines = [
        f"Profitability Attribution {report.get('date', '')} [{report.get('user_id', 'default')}]",
        (
            "Overall PnL: "
            f"realized=${float(overall.get('realized') or 0.0):.2f} "
            f"unrealized=${float(overall.get('unrealized') or 0.0):.2f} "
            f"total=${float(overall.get('total') or 0.0):.2f}"
        ),
        "",
        "PnL by route:",
    ]
    for route in ROUTE_BUCKETS:
        lines.append(f"  {route}: ${float(pnl_by_route.get(route) or 0.0):.2f}")
    lines.extend(["", "Trade stats per route:"])
    for route in ROUTE_BUCKETS:
        stats = route_stats.get(route) if isinstance(route_stats.get(route), Mapping) else {}
        profit_factor = stats.get("profit_factor")
        pf_text = "n/a" if profit_factor is None else f"{float(profit_factor):.2f}"
        lines.append(
            "  "
            f"{route}: trades={int(stats.get('trades') or 0)} "
            f"wins={int(stats.get('wins') or 0)} "
            f"losses={int(stats.get('losses') or 0)} "
            f"win_rate={float(stats.get('win_rate') or 0.0) * 100:.1f}% "
            f"avg_gain=${float(stats.get('avg_gain') or 0.0):.2f} "
            f"avg_loss=${float(stats.get('avg_loss') or 0.0):.2f} "
            f"profit_factor={pf_text}"
        )
    for label, key in (("Top winners", "top_winners"), ("Top losers", "top_losers")):
        lines.extend(["", f"{label}:"])
        rows = report.get(key) if isinstance(report.get(key), list) else []
        if not rows:
            lines.append("  none")
            continue
        for row in rows[:10]:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                "  "
                f"{row.get('symbol', 'UNKNOWN')} {row.get('route', 'unknown')} "
                f"${float(row.get('pnl') or 0.0):.2f}"
            )
    return "\n".join(lines)
