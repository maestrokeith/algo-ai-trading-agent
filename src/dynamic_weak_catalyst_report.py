"""Read-only dynamic weak-catalyst monitoring report."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from src.trade_attribution import attribution_daily_path


_KEY_VALUE_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if out == out else default


def _day_text(day: date | str) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def _compact_day(day: date | str) -> str:
    return _day_text(day).replace("-", "")


def _line_matches_day(line: str, day: date | str) -> bool:
    day_s = _day_text(day)
    compact = _compact_day(day)
    return day_s in line or compact in line or not re.search(r"\d{4}-\d{2}-\d{2}", line)


def _parse_key_values(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _KEY_VALUE_RE.finditer(line):
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[match.group("key")] = value
    return out


def _symbol_from_line(line: str) -> str:
    kv = _parse_key_values(line)
    symbol = str(kv.get("symbol") or kv.get("sym") or "").strip().upper()
    if symbol:
        return symbol
    match = re.search(r"\b(?:symbol|sym)=([A-Z][A-Z0-9.\-]{0,9})\b", line)
    return match.group(1).upper() if match else ""


def _load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rows(payload: Any, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _local_log_lines(data_dir: Path, day: date | str) -> list[str]:
    project_root = data_dir.parent if data_dir.name == "data" else data_dir
    roots = (
        data_dir / "logs",
        data_dir / "debug_logs",
        data_dir / "review",
        project_root / "logs",
        project_root / "data" / "review",
        project_root / "reports" / "debug",
    )
    day_s = _day_text(day)
    compact = _compact_day(day)
    lines: list[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".log", ".txt", ".out"}:
                continue
            if day_s not in str(path) and compact not in str(path) and "latest" not in path.name:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lines.extend(line for line in text.splitlines() if _line_matches_day(line, day))
    return lines


def _is_dynamic(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in ("route", "entry_route", "strategy", "source", "entry_source")
    ).lower()
    return "dynamic" in text or "momentum" in text


def _is_strong_catalyst(row: Mapping[str, Any]) -> bool:
    news = max(_safe_float(row.get("news_score")), _safe_float(row.get("event_score")))
    catalyst = _safe_float(row.get("catalyst_score"))
    return news >= 7.0 or catalyst >= 0.80


def _trade_pnl(row: Mapping[str, Any]) -> float:
    for key in ("pnl", "realized_pnl", "profit_loss", "realized_profit_loss", "realized_pl"):
        if row.get(key) is not None:
            return _safe_float(row.get(key))
    return 0.0


def _trade_symbol(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("sym") or "").strip().upper()


def _notional_from_kv(kv: Mapping[str, str]) -> float:
    for key in ("reduced_notional", "notional_after", "after_notional", "final_notional", "notional", "target_notional"):
        if kv.get(key) is not None:
            value = _safe_float(kv.get(key))
            if value > 0:
                return value
    return 0.0


def build_dynamic_weak_catalyst_report(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    log_text: str | None = None,
) -> dict[str, Any]:
    """Build the weak-catalyst review from logs and trade attribution artifacts."""
    data = Path(data_dir)
    lines = str(log_text or "").splitlines() if log_text is not None else _local_log_lines(data, day)
    lines = [line for line in lines if _line_matches_day(str(line), day)]
    classified_symbols: set[str] = set()
    rejected_counts: Counter[str] = Counter()
    size_reduced_symbols: set[str] = set()
    reduced_notionals: list[float] = []
    weak_order_symbols: set[str] = set()
    weak_fill_symbols: set[str] = set()
    strong_symbols: set[str] = set()

    for raw in lines:
        line = str(raw)
        symbol = _symbol_from_line(line)
        kv = _parse_key_values(line)
        if "DYNAMIC_WEAK_CATALYST_CLASSIFIED" in line and symbol:
            classified_symbols.add(symbol)
        if "DYNAMIC_WEAK_CATALYST_REJECT" in line and symbol:
            rejected_counts[symbol] += 1
        if "DYNAMIC_WEAK_CATALYST_SIZE_REDUCED" in line and symbol:
            size_reduced_symbols.add(symbol)
            classified_symbols.add(symbol)
            notional = _notional_from_kv(kv)
            if notional > 0:
                reduced_notionals.append(notional)
        if "DYNAMIC_WEAK_CATALYST" in line and "ORDER" in line and symbol:
            weak_order_symbols.add(symbol)
        if "DYNAMIC_STRONG_CATALYST" in line and symbol:
            strong_symbols.add(symbol)

    weak_symbols = set(classified_symbols) | set(size_reduced_symbols)
    for raw in lines:
        line = str(raw)
        symbol = _symbol_from_line(line)
        if not symbol:
            continue
        kv = _parse_key_values(line)
        weak_flag = str(kv.get("weak_catalyst") or kv.get("dynamic_weak_catalyst") or "").lower() in {"1", "true", "yes"}
        if symbol in weak_symbols or weak_flag:
            if "ORDER_SUBMITTED" in line or "ALLOCATOR_ACTION_SUBMITTED" in line:
                weak_order_symbols.add(symbol)
                notional = _notional_from_kv(kv)
                if symbol in size_reduced_symbols and notional > 0:
                    reduced_notionals.append(notional)
            if "ORDER_FILLED" in line:
                weak_fill_symbols.add(symbol)
        if str(kv.get("weak_catalyst") or "").lower() in {"0", "false"} and _safe_float(kv.get("catalyst_score")) >= 0.8:
            strong_symbols.add(symbol)

    payload = _load_json(attribution_daily_path(data_dir=data, user_id=user_id, day=_day_text(day)))
    trades = _rows(payload, ("exits", "trades", "filled_orders", "orders"))
    weak_trades: list[Mapping[str, Any]] = []
    strong_trade_count = 0
    for row in trades:
        symbol = _trade_symbol(row)
        if not symbol:
            continue
        if _is_dynamic(row) and _is_strong_catalyst(row):
            strong_trade_count += 1
            strong_symbols.add(symbol)
            continue
        weak_flag = bool(row.get("weak_catalyst") or row.get("dynamic_weak_catalyst"))
        if symbol in weak_symbols or weak_flag:
            weak_trades.append(row)
            weak_symbols.add(symbol)
    pnl = sum(_trade_pnl(row) for row in weak_trades)
    loser_rows = sorted(
        (
            {"symbol": _trade_symbol(row), "pnl": _trade_pnl(row)}
            for row in weak_trades
            if _trade_pnl(row) < 0
        ),
        key=lambda row: float(row["pnl"]),
    )
    rejected = [{"symbol": sym, "count": count} for sym, count in rejected_counts.most_common(5)]
    orders = len(weak_order_symbols)
    if not orders and weak_trades:
        orders = len({_trade_symbol(row) for row in weak_trades if _trade_symbol(row)})
    dynamic_losses = sum(1 for row in trades if _is_dynamic(row) and _trade_pnl(row) < 0)
    if strong_trade_count == 0 and any(_safe_float(row.get("catalyst_score")) >= 0.8 for row in trades):
        recommendation = "review strong-catalyst bypass"
    elif pnl < 0.0 and orders > 3:
        recommendation = "tighten RVOL or smaller starter"
    elif rejected_counts and strong_trade_count > 0:
        recommendation = "leave unchanged"
    elif orders == 0 and dynamic_losses == 0:
        recommendation = "leave unchanged"
    else:
        recommendation = "leave unchanged"
    avg_notional = (sum(reduced_notionals) / len(reduced_notionals)) if reduced_notionals else 0.0
    return {
        "date": _day_text(day),
        "user_id": str(user_id or "default"),
        "classified": len(classified_symbols),
        "rejected": sum(rejected_counts.values()),
        "size_reduced": len(size_reduced_symbols),
        "orders": orders,
        "fills": len(weak_fill_symbols),
        "realized_pnl": round(pnl, 6),
        "avg_notional": round(avg_notional, 6),
        "top_weak_catalyst_losers": loser_rows[:5],
        "top_rejected_weak_catalyst_names": rejected,
        "strong_catalyst_trades_unchanged_count": int(strong_trade_count),
        "recommendation": recommendation,
    }


def format_dynamic_weak_catalyst_report(report: Mapping[str, Any]) -> str:
    """Render the weak-catalyst review section."""
    losers = report.get("top_weak_catalyst_losers") if isinstance(report.get("top_weak_catalyst_losers"), list) else []
    rejected = (
        report.get("top_rejected_weak_catalyst_names")
        if isinstance(report.get("top_rejected_weak_catalyst_names"), list)
        else []
    )
    loser_text = ",".join(
        f"{row.get('symbol')}:{_safe_float(row.get('pnl')):.2f}"
        for row in losers
        if isinstance(row, Mapping)
    ) or "none"
    rejected_text = ",".join(
        f"{row.get('symbol')}:{int(_safe_float(row.get('count')))}"
        for row in rejected
        if isinstance(row, Mapping)
    ) or "none"
    return "\n".join(
        [
            "DYNAMIC_WEAK_CATALYST_REVIEW",
            f"- classified: {int(_safe_float(report.get('classified')))}",
            f"- rejected: {int(_safe_float(report.get('rejected')))}",
            f"- size_reduced: {int(_safe_float(report.get('size_reduced')))}",
            f"- orders: {int(_safe_float(report.get('orders')))}",
            f"- fills: {int(_safe_float(report.get('fills')))}",
            f"- realized_pnl: {_safe_float(report.get('realized_pnl')):.2f}",
            f"- avg_notional: {_safe_float(report.get('avg_notional')):.2f}",
            f"- top_weak_catalyst_losers: {loser_text}",
            f"- top_rejected_weak_catalyst_names: {rejected_text}",
            f"- strong_catalyst_trades_unchanged_count: {int(_safe_float(report.get('strong_catalyst_trades_unchanged_count')))}",
            f"- recommendation: {report.get('recommendation') or 'leave unchanged'}",
        ]
    )


__all__ = [
    "build_dynamic_weak_catalyst_report",
    "format_dynamic_weak_catalyst_report",
]
