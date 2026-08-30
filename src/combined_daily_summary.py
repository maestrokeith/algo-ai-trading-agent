"""Read-only combined daily summary reporting."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.catalyst_outcomes import load_catalyst_outcome_records, summarize_catalyst_outcomes
from src.dynamic_weak_catalyst_report import build_dynamic_weak_catalyst_report
from src.profitability_attribution import (
    ROUTE_BUCKETS,
    build_profitability_report,
    build_trade_churn_analysis,
    discover_replay_summary_path,
    load_profitability_report_inputs,
)
from src.trade_postmortem import build_daily_postmortem


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _load_json(path: Path | str | None) -> Any | None:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _risk_guard_summary(data_dir: Path | str, *, user_id: str, day: date | str) -> dict[str, Any] | None:
    path = Path(data_dir) / "risk_guards" / f"{_day_text(day)}_{user_id}.json"
    payload = _load_json(path)
    return dict(payload) if isinstance(payload, Mapping) else None


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


def _first_value(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _route_label(row: Mapping[str, Any]) -> str:
    return str(
        _first_value(
            row,
            (
                "strategy",
                "route",
                "entry_route",
                "entry_source",
                "source",
            ),
        )
        or "unknown"
    )


def _day_text(day: date | str) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def _compact_day(day: date | str) -> str:
    return _day_text(day).replace("-", "")


def _line_matches_day(line: str, day: date | str) -> bool:
    day_s = _day_text(day)
    compact = _compact_day(day)
    return day_s in line or compact in line or not re.search(r"\d{4}-\d{2}-\d{2}", line)


def _read_local_live_log_lines(data_dir: Path | str, day: date | str) -> list[str]:
    data = Path(data_dir)
    project_root = data.parent if data.name == "data" else data
    roots = (
        data / "logs",
        data / "debug_logs",
        data / "review",
        project_root / "logs",
        project_root / "data" / "review",
        project_root / "reports" / "debug",
    )
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        candidates.extend(
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in {".log", ".txt", ".out"}
        )
    lines: list[str] = []
    day_s = _day_text(day)
    compact = _compact_day(day)
    for path in sorted(set(candidates)):
        if day_s not in str(path) and compact not in str(path) and "latest" not in path.name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines.extend(line for line in text.splitlines() if _line_matches_day(line, day))
    return lines


def _read_journal_lines(day: date | str, *, unit: str | None) -> tuple[list[str], str | None]:
    if not unit:
        return [], None
    if shutil.which("journalctl") is None:
        return [], "journalctl_unavailable"
    day_s = _day_text(day)
    try:
        start = date.fromisoformat(day_s)
    except ValueError:
        return [], "journalctl_bad_date"
    end = start + timedelta(days=1)
    cmd = (
        "journalctl",
        "-u",
        str(unit),
        "--since",
        f"{start.isoformat()} 00:00:00",
        "--until",
        f"{end.isoformat()} 00:00:00",
        "--no-pager",
    )
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        return [], "journalctl_timeout"
    except OSError as exc:
        return [], f"journalctl_error:{type(exc).__name__}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "journalctl_failed").strip().splitlines()
        return [], "journalctl_failed:" + (detail[0] if detail else "unknown")
    return [line for line in proc.stdout.splitlines() if _line_matches_day(line, day)], None


_KEY_VALUE_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>\"[^\"]*\"|'[^']*'|[^\s]+)")
_SELL_SHARES_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+SELL\s+(?P<qty>[0-9.]+)\s+shares\b(?P<tail>.*)",
    re.IGNORECASE,
)


def _parse_key_values(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in _KEY_VALUE_RE.finditer(line):
        value = match.group("value").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        out[match.group("key")] = value
    return out


def _canonical_route(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).strip().lower()
    if "dynamic_eod_flatten" in text:
        return "dynamic_eod_flatten"
    if "dynamic_momentum_override" in text:
        return "dynamic_momentum_override"
    if "dynamic_universe" in text:
        return "dynamic_universe"
    if "dynamic" in text or "momentum" in text:
        return "dynamic_momentum"
    if "core_rebuild" in text:
        return "core_rebuild"
    if "trend_long" in text:
        return "trend_long"
    return str(values[0] or values[1] or "unknown").strip() or "unknown"


def _sell_exit_reason(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).strip().lower()
    if "dynamic_eod_flatten" in text:
        return "dynamic_eod_flatten"
    if "stop_loss" in text or "stop loss" in text:
        return "stop_loss"
    if "take_profit" in text or "take profit" in text:
        return "take_profit"
    if "trailing_stop" in text or "trailing stop" in text:
        return "trailing_stop"
    if "signal_exit" in text or "signal exit" in text:
        return "signal_exit"
    if "reason=" in text:
        reason = _parse_key_values(text).get("reason")
        if reason:
            return _sell_exit_reason(reason)
    return "sell"


def _dedupe_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        order_id = row.get("order_id")
        order_id_text = str(order_id).strip() if order_id is not None else ""
        if order_id_text and order_id_text.lower() != "none":
            key = (
                order_id_text,
                str(row.get("symbol") or "").upper(),
                str(row.get("action") or row.get("side") or "").lower(),
            )
        else:
            key = (
                "",
                str(row.get("symbol") or "").upper(),
                str(row.get("action") or row.get("side") or "").lower(),
                str(row.get("timestamp") or ""),
                str(row.get("notional") or row.get("qty") or ""),
            )
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def _live_activity_from_log_lines(lines: Sequence[str], *, day: date | str) -> dict[str, Any]:
    orders: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    route_by_symbol: dict[str, str] = {}
    metadata_by_symbol: dict[str, dict[str, Any]] = {}
    filled_by_order_id: dict[str, dict[str, Any]] = {}
    fill_rows: list[dict[str, Any]] = []
    for raw in lines:
        line = str(raw)
        if not _line_matches_day(line, day):
            continue
        kv = _parse_key_values(line)
        symbol = str(kv.get("symbol") or "").strip().upper()
        route = _canonical_route(kv.get("route"), kv.get("source"), line)
        if symbol and route != "unknown":
            prior_route = route_by_symbol.get(symbol)
            if not (str(prior_route or "").startswith("dynamic_") and route == "capital_allocator"):
                route_by_symbol[symbol] = route
        if symbol:
            meta = metadata_by_symbol.setdefault(symbol, {})
            if route != "unknown":
                prior_meta_route = str(meta.get("route") or "")
                if not (prior_meta_route.startswith("dynamic_") and route == "capital_allocator"):
                    meta["route"] = route
            for src_key, dst_key in (
                ("news_score", "news_score"),
                ("catalyst_score", "catalyst_score"),
                ("event_score", "event_score"),
                ("relative_volume", "relative_volume"),
                ("rel", "relative_volume"),
                ("rel_volume", "relative_volume"),
                ("gain", "gain_pct"),
                ("gain_pct", "gain_pct"),
                ("catalyst_type", "catalyst_type"),
            ):
                if src_key in kv and kv[src_key] is not None:
                    value: Any = kv[src_key]
                    num = _safe_float(value, float("nan"))
                    meta[dst_key] = value if str(num) == "nan" else num
        if "ALLOCATOR_ACTION_SUBMITTED" in line or "ORDER_SUBMITTED" in line:
            action = str(kv.get("action") or kv.get("side") or "").strip().lower()
            if action not in {"buy", "sell"}:
                continue
            if not symbol:
                continue
            order_route = _canonical_route(kv.get("route"), route_by_symbol.get(symbol), kv.get("source"), line)
            order_row = {
                "symbol": symbol,
                "action": action,
                "side": action,
                "route": order_route,
                "source": kv.get("source") or order_route,
                "notional": _safe_float(kv.get("notional"), 0.0) if kv.get("notional") is not None else None,
                "qty": _safe_float(kv.get("qty"), 0.0) if kv.get("qty") is not None else None,
                "order_id": kv.get("order_id"),
                "status": kv.get("status"),
                "submitted": True,
                "timestamp": line[:25].strip(),
                "source_line": line.strip(),
            }
            order_row.update(metadata_by_symbol.get(symbol, {}))
            orders.append(order_row)
            if action == "sell":
                exit_row = {
                    "symbol": symbol,
                    "entry_route": order_route,
                    "entry_source": kv.get("source") or order_route,
                    "exit_reason": _sell_exit_reason(line, kv.get("reason")),
                    "qty": _safe_float(kv.get("qty"), 0.0) if kv.get("qty") is not None else None,
                    "order_id": kv.get("order_id"),
                    "pnl": None,
                    "pnl_missing": True,
                    "timestamp": line[:25].strip(),
                    "source_line": line.strip(),
                }
                exit_row.update(metadata_by_symbol.get(symbol, {}))
                exits.append(exit_row)
        elif "ORDER_FILLED" in line:
            order_id = str(kv.get("order_id") or "").strip()
            action = str(kv.get("action") or kv.get("side") or "").strip().lower()
            fill_row = {
                "symbol": symbol,
                "action": action,
                "side": action,
                "order_id": order_id or None,
                "filled_qty": _safe_float(kv.get("filled_qty"), 0.0),
                "filled_avg_price": _safe_float(kv.get("filled_avg_price"), 0.0),
                "timestamp": line[:25].strip(),
                "source_line": line.strip(),
            }
            if symbol and action in {"buy", "sell"}:
                fill_rows.append(fill_row)
            if order_id:
                filled_by_order_id[order_id] = fill_row
            if symbol and action == "sell":
                order_route = _canonical_route(kv.get("route"), route_by_symbol.get(symbol), kv.get("source"), line)
                exit_row = {
                    "symbol": symbol,
                    "entry_route": order_route,
                    "entry_source": kv.get("source") or order_route,
                    "exit_reason": _sell_exit_reason(line, kv.get("reason")),
                    "qty": _safe_float(kv.get("filled_qty"), 0.0),
                    "order_id": order_id or None,
                    "filled_qty": _safe_float(kv.get("filled_qty"), 0.0),
                    "filled_avg_price": _safe_float(kv.get("filled_avg_price"), 0.0),
                    "pnl": None,
                    "pnl_missing": True,
                    "timestamp": line[:25].strip(),
                    "source_line": line.strip(),
                    "count_source": "broker_fill_log",
                }
                exit_row.update(metadata_by_symbol.get(symbol, {}))
                exits.append(exit_row)
        sell_match = _SELL_SHARES_RE.search(line)
        if sell_match:
            sell_symbol = sell_match.group("symbol").upper()
            tail = sell_match.group("tail") or ""
            exit_reason = _sell_exit_reason(tail)
            order_route = _canonical_route(exit_reason, route_by_symbol.get(sell_symbol), line)
            exit_row = {
                "symbol": sell_symbol,
                "entry_route": order_route,
                "entry_source": order_route,
                "exit_reason": exit_reason,
                "qty": _safe_float(sell_match.group("qty"), 0.0),
                "pnl": None,
                "pnl_missing": True,
                "timestamp": line[:25].strip(),
                "source_line": line.strip(),
            }
            exit_row.update(metadata_by_symbol.get(sell_symbol, {}))
            exits.append(exit_row)
    for row in orders:
        order_id = str(row.get("order_id") or "").strip()
        if order_id and order_id in filled_by_order_id:
            row.update(filled_by_order_id[order_id])
    _attach_fill_realized_pnl(exits, fill_rows)
    return {
        "version": 1,
        "date": _day_text(day),
        "orders": _dedupe_rows(orders),
        "exits": _dedupe_rows(exits),
        "summary": {
            "source": "live_logs",
            "log_lines_scanned": len(lines),
            "filled_orders": len(fill_rows),
        },
    }


def _attach_fill_realized_pnl(exits: Sequence[dict[str, Any]], fill_rows: Sequence[Mapping[str, Any]]) -> None:
    buy_cost: dict[str, float] = {}
    buy_qty: dict[str, float] = {}
    sell_by_order_id: dict[str, Mapping[str, Any]] = {}
    sell_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for fill in fill_rows:
        symbol = str(fill.get("symbol") or "").strip().upper()
        side = str(fill.get("side") or fill.get("action") or "").strip().lower()
        qty = _safe_float(fill.get("filled_qty"), 0.0)
        price = _safe_float(fill.get("filled_avg_price"), 0.0)
        if not symbol or qty <= 0.0 or price <= 0.0:
            continue
        if side == "buy":
            buy_cost[symbol] = buy_cost.get(symbol, 0.0) + qty * price
            buy_qty[symbol] = buy_qty.get(symbol, 0.0) + qty
        elif side == "sell":
            order_id = str(fill.get("order_id") or "").strip()
            if order_id:
                sell_by_order_id[order_id] = fill
            sell_by_symbol.setdefault(symbol, []).append(fill)
    avg_buy = {
        symbol: buy_cost[symbol] / qty
        for symbol, qty in buy_qty.items()
        if qty > 0.0
    }
    used_sell_ids: set[str] = set()
    for exit_row in exits:
        if _first_value(exit_row, ("pnl", "realized_pnl", "profit_loss")) is not None:
            exit_row["pnl_missing"] = False
            continue
        symbol = str(exit_row.get("symbol") or "").strip().upper()
        if symbol not in avg_buy:
            continue
        order_id = str(exit_row.get("order_id") or "").strip()
        fill: Mapping[str, Any] | None = sell_by_order_id.get(order_id) if order_id else None
        if fill is None:
            for candidate in sell_by_symbol.get(symbol, []):
                candidate_id = str(candidate.get("order_id") or "")
                if candidate_id and candidate_id in used_sell_ids:
                    continue
                fill = candidate
                break
        if fill is None:
            continue
        fill_id = str(fill.get("order_id") or "")
        if fill_id:
            used_sell_ids.add(fill_id)
        qty = _safe_float(fill.get("filled_qty"), 0.0)
        sell_price = _safe_float(fill.get("filled_avg_price"), 0.0)
        if qty <= 0.0 or sell_price <= 0.0:
            continue
        pnl = (sell_price - avg_buy[symbol]) * qty
        exit_row["pnl"] = round(pnl, 6)
        exit_row["pnl_missing"] = False
        exit_row["pnl_source"] = "broker_fill_log"
        exit_row["filled_qty"] = qty
        exit_row["filled_avg_price"] = sell_price


def load_live_activity_payload(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    include_journal: bool = False,
    journalctl_unit: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    local_lines = _read_local_live_log_lines(data_dir, day)
    journal_lines: list[str] = []
    journal_warning: str | None = None
    if include_journal:
        journal_lines, journal_warning = _read_journal_lines(day, unit=journalctl_unit)
    payload = _live_activity_from_log_lines([*local_lines, *journal_lines], day=day)
    diagnostics = {
        "local_log_lines": len(local_lines),
        "journal_log_lines": len(journal_lines),
        "journal_warning": journal_warning,
        "live_log_orders": len(payload.get("orders") or []),
        "live_log_exits": len(payload.get("exits") or []),
    }
    if not payload.get("orders") and not payload.get("exits"):
        return None, diagnostics
    payload["user_id"] = str(user_id or "default")
    payload["diagnostics"] = diagnostics
    return payload, diagnostics


def _merge_activity_payloads(*payloads: Mapping[str, Any] | None) -> dict[str, Any] | None:
    merged: dict[str, Any] = {"orders": [], "exits": []}
    available = False
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        available = True
        for key in ("orders", "exits", "candidates", "allocator_candidates"):
            rows = payload.get(key)
            if isinstance(rows, list):
                merged.setdefault(key, []).extend(row for row in rows if isinstance(row, Mapping))
    if not available:
        return None
    for key in ("orders", "exits"):
        merged[key] = _dedupe_rows(merged.get(key, []))
    return merged


def _order_activity_summary(activity_payload: Mapping[str, Any] | None, diagnostics: Mapping[str, Any] | None) -> dict[str, Any]:
    orders = _rows(activity_payload or {}, ("orders",))
    exits = _rows(activity_payload or {}, ("exits",))
    submitted = [
        row for row in orders
        if row.get("submitted") is True or str(row.get("status") or "").lower() in {"accepted", "filled", "submitted", "n/a"}
    ]
    buy_symbols = [str(row.get("symbol") or "").upper() for row in submitted if str(row.get("action") or row.get("side") or "").lower() == "buy"]
    submitted_sell_symbols = [
        str(row.get("symbol") or "").upper()
        for row in submitted
        if str(row.get("action") or row.get("side") or "").lower() == "sell"
    ]
    exit_symbols = [str(row.get("symbol") or "").upper() for row in exits]
    sell_symbols = [*submitted_sell_symbols, *exit_symbols]
    route_counts = Counter(
        _canonical_route(row.get("route"), row.get("source"), row.get("entry_route"))
        for row in [*submitted, *exits]
    )
    pnl_missing = sum(1 for row in exits if _first_value(row, ("pnl", "realized_pnl", "profit_loss")) is None)
    symbols = sorted({sym for sym in [*buy_symbols, *sell_symbols, *(str(row.get("symbol") or "").upper() for row in exits)] if sym})
    return {
        "submitted_orders": len(submitted),
        "buy_orders": len(buy_symbols),
        "sell_orders": len(sell_symbols),
        "exit_records": len(exits),
        "symbols": symbols,
        "buy_symbols": sorted(set(buy_symbols)),
        "sell_symbols": sorted(set(sell_symbols)),
        "route_counts": dict(sorted(route_counts.items())),
        "pnl_missing_exits": pnl_missing,
        "diagnostics": dict(diagnostics or {}),
    }


def _mfe_mae_summary(activity_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    exits = _rows(activity_payload or {}, ("exits", "trades", "orders"))
    mfe_values = [_safe_float(row.get("mfe_pct"), float("nan")) for row in exits if isinstance(row, Mapping)]
    mae_values = [_safe_float(row.get("mae_pct"), float("nan")) for row in exits if isinstance(row, Mapping)]
    mfe_values = [value for value in mfe_values if value == value]
    mae_values = [value for value in mae_values if value == value]
    return {
        "available": bool(mfe_values or mae_values),
        "avg_mfe_pct": sum(mfe_values) / len(mfe_values) if mfe_values else None,
        "avg_mae_pct": sum(mae_values) / len(mae_values) if mae_values else None,
        "count": max(len(mfe_values), len(mae_values)),
    }


def _source_count_diagnostics(
    *,
    attribution_payload: Mapping[str, Any] | None,
    order_history_payload: Any | None,
    live_activity_payload: Mapping[str, Any] | None,
    live_activity_diag: Mapping[str, Any] | None,
) -> dict[str, Any]:
    order_history_mapping = order_history_payload if isinstance(order_history_payload, Mapping) else {}
    live_summary = (
        live_activity_payload.get("summary")
        if isinstance(live_activity_payload, Mapping) and isinstance(live_activity_payload.get("summary"), Mapping)
        else {}
    )
    return {
        "attribution_orders": len(_rows(attribution_payload or {}, ("orders",))),
        "attribution_exits": len(_rows(attribution_payload or {}, ("exits",))),
        "order_history_orders": len(_rows(order_history_mapping, ("orders", "trades", "filled_orders", "order_history"))),
        "live_log_orders": int((live_activity_diag or {}).get("live_log_orders") or 0),
        "live_log_exits": int((live_activity_diag or {}).get("live_log_exits") or 0),
        "broker_fill_logs": int(live_summary.get("filled_orders") or 0),
        "local_log_lines": int((live_activity_diag or {}).get("local_log_lines") or 0),
        "journal_log_lines": int((live_activity_diag or {}).get("journal_log_lines") or 0),
    }


def _broker_open_unrealized_summary(daily_summary_payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(daily_summary_payload, Mapping):
        return {
            "available": False,
            "source": "trade_attribution_only",
            "unrealized": 0.0,
            "positions": 0,
            "symbols": [],
        }
    positions = daily_summary_payload.get("positions")
    if not isinstance(positions, list):
        return {
            "available": False,
            "source": "trade_attribution_only",
            "unrealized": 0.0,
            "positions": 0,
            "symbols": [],
        }
    rows = [row for row in positions if isinstance(row, Mapping)]
    finite = [
        _safe_float(raw)
        for row in rows
        if (raw := _first_value(row, ("unrealized_pl", "unrealized_pnl", "pnl"))) is not None
    ]
    symbols = sorted({str(row.get("symbol") or "").strip().upper() for row in rows if str(row.get("symbol") or "").strip()})
    return {
        "available": bool(finite),
        "source": "broker_open_positions" if finite else "trade_attribution_only",
        "unrealized": round(sum(finite), 6) if finite else 0.0,
        "positions": len(rows),
        "symbols": symbols,
    }


def _postmortem_trades(
    *,
    attribution_payload: Mapping[str, Any] | None,
    order_history_payload: Any | None,
    activity_payload: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return realized trades in the shape expected by trade postmortem."""
    source_rows = _rows(attribution_payload or {}, ("exits",))
    if not source_rows:
        source_rows = _rows(order_history_payload, ("orders", "trades", "filled_orders", "order_history"))
    if not source_rows:
        source_rows = _rows(activity_payload or {}, ("exits",))
    if not source_rows:
        source_rows = [
            row for row in _rows(activity_payload or {}, ("orders",))
            if row.get("submitted") is True
        ]
    out: list[dict[str, Any]] = []
    for row in source_rows:
        row_route = _canonical_route(
            row.get("route"),
            row.get("entry_route"),
            row.get("strategy"),
            row.get("source"),
            row.get("entry_source"),
            row.get("exit_reason"),
            row.get("reason"),
        )
        if row_route in {"dynamic_momentum_override", "dynamic_eod_flatten", "dynamic_universe"}:
            row_route = "dynamic_momentum"
        pnl = _first_value(
            row,
            (
                "pnl",
                "realized_pnl",
                "profit_loss",
                "realized_profit_loss",
                "realized_pl",
            ),
        )
        pnl_missing = pnl is None
        out.append(
            {
                "id": str(_first_value(row, ("id", "trade_id", "order_id")) or ""),
                "symbol": str(row.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN",
                "strategy": row_route,
                "source": str(row.get("source") or ""),
                "pnl": 0.0 if pnl_missing else _safe_float(pnl),
                "pnl_missing": pnl_missing,
                "return_pct": _first_value(
                    row,
                    ("return_pct", "pnl_pct", "realized_return_pct", "profit_loss_pct"),
                ),
                "qty": row.get("qty"),
                "filled_avg_price": row.get("filled_avg_price"),
                "catalyst_type": _first_value(row, ("catalyst_type", "news_catalyst_type", "event_type")),
                "news_score": _first_value(row, ("news_score", "entry_news_score")),
                "event_score": _first_value(row, ("event_score", "entry_event_score")),
                "catalyst_score": _first_value(row, ("catalyst_score", "entry_catalyst_score")),
                "relative_volume": _first_value(row, ("relative_volume", "rel_volume", "entry_relative_volume")),
                "hold_minutes": _first_value(row, ("hold_minutes", "duration_minutes", "hold_duration_minutes")),
                "exit_reason": _first_value(row, ("exit_reason", "reason", "sell_reason", "exit_type")),
                "max_favorable_excursion_pct": _first_value(
                    row,
                    ("max_favorable_excursion_pct", "missed_return_pct"),
                ),
                "avoidable_loss": row.get("avoidable_loss"),
            }
        )
    return out


def _weak_catalyst_dynamic_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    weak_rows: list[Mapping[str, Any]] = []
    for row in rows:
        route = _canonical_route(row.get("strategy"), row.get("route"), row.get("entry_route"), row.get("source"))
        if route not in {"dynamic_momentum", "dynamic_momentum_override", "dynamic_universe"}:
            continue
        news = _safe_float(_first_value(row, ("news_score", "entry_news_score")))
        event = _safe_float(_first_value(row, ("event_score", "entry_event_score")))
        catalyst = _safe_float(_first_value(row, ("catalyst_score", "entry_catalyst_score")))
        if abs(news) <= 1e-9 and abs(event) <= 1e-9 and abs(catalyst) <= 1e-9:
            weak_rows.append(row)
    realized = [
        row for row in weak_rows
        if _first_value(row, ("pnl", "realized_pnl", "profit_loss", "realized_profit_loss", "realized_pl")) is not None
    ]
    pnl_values = [
        _safe_float(_first_value(row, ("pnl", "realized_pnl", "profit_loss", "realized_profit_loss", "realized_pl")))
        for row in realized
    ]
    wins = sum(1 for value in pnl_values if value > 0.0)
    return {
        "trades": len(weak_rows),
        "realized_trades": len(realized),
        "pnl": round(sum(pnl_values), 6),
        "win_rate": (wins / len(pnl_values)) if pnl_values else 0.0,
        "symbols": sorted({str(row.get("symbol") or "").upper() for row in weak_rows if str(row.get("symbol") or "").strip()}),
    }


def _fmt_money(value: Any) -> str:
    return f"${_safe_float(value):,.2f}"


def _fmt_pct(value: Any, *, fraction: bool = False) -> str:
    raw = _safe_float(value)
    pct = raw * 100.0 if fraction else raw
    return f"{pct:.1f}%"


def _fmt_pf(value: Any) -> str:
    if value is None:
        return "n/a"
    out = _safe_float(value)
    return "inf" if out == float("inf") else f"{out:.2f}"


def _top_routes(report: Mapping[str, Any], *, limit: int = 3) -> list[str]:
    pnl_by_route = report.get("pnl_by_route") if isinstance(report.get("pnl_by_route"), Mapping) else {}
    rows = [(route, _safe_float(pnl_by_route.get(route))) for route in ROUTE_BUCKETS]
    return [f"{route} {_fmt_money(pnl)}" for route, pnl in sorted(rows, key=lambda item: abs(item[1]), reverse=True)[:limit]]


def _top_catalysts(
    catalyst_summary: Mapping[str, Mapping[str, float]],
    *,
    limit: int = 3,
) -> list[str]:
    rows = sorted(
        catalyst_summary.items(),
        key=lambda item: _safe_float(item[1].get("sample_count", item[1].get("count", 0.0))),
        reverse=True,
    )
    return [
        (
            f"{name} n={int(_safe_float(stats.get('sample_count', stats.get('count', 0.0))))} "
            f"win={_fmt_pct(stats.get('win_rate_pct'))} "
            f"avg={_fmt_pct(stats.get('avg_return_pct'))} "
            f"pf={_fmt_pf(stats.get('profit_factor'))}"
        )
        for name, stats in rows[:limit]
    ]


def _replay_summary_lines(replay_payload: Mapping[str, Any] | None) -> list[str]:
    if not replay_payload:
        return ["Replay: no replay summary found."]
    clock = replay_payload.get("clock") if isinstance(replay_payload.get("clock"), Mapping) else {}
    churn = (
        replay_payload.get("churn_same_day_reversal_stats")
        if isinstance(replay_payload.get("churn_same_day_reversal_stats"), Mapping)
        else {}
    )
    route_pnl = (
        replay_payload.get("route_level_pnl_estimate")
        if isinstance(replay_payload.get("route_level_pnl_estimate"), Mapping)
        else {}
    )
    route_text = ", ".join(
        f"{route} {_fmt_money(pnl)}"
        for route, pnl in sorted(route_pnl.items(), key=lambda item: abs(_safe_float(item[1])), reverse=True)[:3]
    )
    if not route_text:
        route_text = "none"
    return [
        (
            "Replay: "
            f"ticks={int(_safe_float(clock.get('tick_count')))} "
            f"cycles={int(_safe_float(clock.get('cycles_with_data')))} "
            f"mock_orders={len(replay_payload.get('mock_orders') or [])} "
            f"selected={len(replay_payload.get('selected_candidates') or [])} "
            f"rejected={len(replay_payload.get('rejected_candidates') or [])}"
        ),
        (
            "Replay churn: "
            f"reversals={int(_safe_float(churn.get('same_day_reversal_count')))} "
            f"repeat_orders={int(_safe_float(churn.get('repeat_order_count')))} "
            f"route_pnl={route_text}"
        ),
    ]


def build_combined_daily_summary(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
    catalyst_path: Path | str | None = None,
    order_history_path: Path | str | None = None,
    daily_summary_path: Path | str | None = None,
    replay_summary_path: Path | str | None = None,
    include_journal: bool = False,
    journalctl_unit: str | None = None,
) -> dict[str, Any]:
    """Build the combined daily summary from read-only local artifacts."""
    attribution, order_history, daily_summary = load_profitability_report_inputs(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
        order_history_path=order_history_path,
        daily_summary_path=daily_summary_path,
    )
    live_activity, live_activity_diag = load_live_activity_payload(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
        include_journal=include_journal,
        journalctl_unit=journalctl_unit,
    )
    replay_path = Path(replay_summary_path) if replay_summary_path else discover_replay_summary_path(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
    )
    replay_payload = _load_json(replay_path)
    activity_payload = _merge_activity_payloads(
        attribution,
        order_history if isinstance(order_history, Mapping) else None,
        live_activity,
    )
    churn = build_trade_churn_analysis(
        user_id=user_id,
        day=day,
        attribution_payload=activity_payload,
        replay_payload=replay_payload if isinstance(replay_payload, Mapping) else None,
    )
    catalyst_store = Path(catalyst_path) if catalyst_path else Path(data_dir) / "analytics" / "catalyst_outcomes.json"
    catalyst_records = [
        row
        for row in load_catalyst_outcome_records(catalyst_store)
        if str(row.get("user_id") or user_id) == str(user_id)
    ]
    catalyst_stats = summarize_catalyst_outcomes(catalyst_records)
    postmortem_trades = _postmortem_trades(
        attribution_payload=attribution,
        order_history_payload=order_history,
        activity_payload=activity_payload,
    )
    profitability = build_profitability_report(
        user_id=user_id,
        day=day,
        attribution_payload=attribution,
        order_history_payload=order_history,
        daily_summary_payload=daily_summary,
    )
    broker_open_unrealized = _broker_open_unrealized_summary(daily_summary)
    route_stats = profitability.get("route_stats") if isinstance(profitability.get("route_stats"), Mapping) else {}
    realized_trade_count = sum(
        int(stats.get("trades") or 0)
        for stats in route_stats.values()
        if isinstance(stats, Mapping)
    )
    if realized_trade_count == 0 and postmortem_trades:
        profitability = build_profitability_report(
            user_id=user_id,
            day=day,
            attribution_payload=None,
            order_history_payload={"trades": postmortem_trades},
            daily_summary_payload=daily_summary,
        )
    source_counts = _source_count_diagnostics(
        attribution_payload=attribution,
        order_history_payload=order_history,
        live_activity_payload=live_activity,
        live_activity_diag=live_activity_diag,
    )
    order_activity = _order_activity_summary(
        activity_payload,
        {**dict(live_activity_diag or {}), "count_sources": source_counts},
    )
    mfe_mae = _mfe_mae_summary(activity_payload)
    weak_catalyst_review = build_dynamic_weak_catalyst_report(
        data_dir=data_dir,
        user_id=user_id,
        day=day,
    )
    risk_guards = _risk_guard_summary(data_dir, user_id=user_id, day=day)
    return {
        "date": _day_text(day),
        "user_id": str(user_id or "default"),
        "inputs": {
            "attribution_available": bool(attribution),
            "order_history_available": order_history is not None,
            "daily_summary_available": daily_summary is not None,
            "live_activity_available": bool(live_activity),
            "catalyst_store": str(catalyst_store),
            "replay_summary": str(replay_path) if replay_path else None,
        },
        "profitability": profitability,
        "broker_open_unrealized": broker_open_unrealized,
        "weak_catalyst_dynamic": _weak_catalyst_dynamic_summary(postmortem_trades),
        "dynamic_weak_catalyst_review": weak_catalyst_review,
        "risk_guards": risk_guards,
        "postmortem": build_daily_postmortem(postmortem_trades),
        "order_activity": order_activity,
        "mfe_mae": mfe_mae,
        "churn": churn,
        "catalyst_stats": catalyst_stats,
        "replay": replay_payload if isinstance(replay_payload, Mapping) else None,
    }


def format_combined_daily_summary(summary: Mapping[str, Any]) -> str:
    """Render a concise combined daily summary for the CLI."""
    profitability = summary.get("profitability") if isinstance(summary.get("profitability"), Mapping) else {}
    overall = profitability.get("overall_pnl") if isinstance(profitability.get("overall_pnl"), Mapping) else {}
    postmortem = summary.get("postmortem")
    churn = summary.get("churn") if isinstance(summary.get("churn"), Mapping) else {}
    catalyst_stats = (
        summary.get("catalyst_stats") if isinstance(summary.get("catalyst_stats"), Mapping) else {}
    )
    route_stats = profitability.get("route_stats") if isinstance(profitability.get("route_stats"), Mapping) else {}
    order_activity = summary.get("order_activity") if isinstance(summary.get("order_activity"), Mapping) else {}
    mfe_mae = summary.get("mfe_mae") if isinstance(summary.get("mfe_mae"), Mapping) else {}
    broker_open = (
        summary.get("broker_open_unrealized")
        if isinstance(summary.get("broker_open_unrealized"), Mapping)
        else {}
    )
    activity_diag = order_activity.get("diagnostics") if isinstance(order_activity.get("diagnostics"), Mapping) else {}
    count_sources = (
        activity_diag.get("count_sources")
        if isinstance(activity_diag.get("count_sources"), Mapping)
        else {}
    )
    reversals = churn.get("same_day_reversals") if isinstance(churn.get("same_day_reversals"), Mapping) else {}
    repeats = churn.get("repeated_activity") if isinstance(churn.get("repeated_activity"), Mapping) else {}
    weak = churn.get("weak_exits") if isinstance(churn.get("weak_exits"), Mapping) else {}
    metrics = getattr(postmortem, "metrics", {}) or {}
    top_winners = profitability.get("top_winners") if isinstance(profitability.get("top_winners"), list) else []
    top_losers = profitability.get("top_losers") if isinstance(profitability.get("top_losers"), list) else []
    weak_catalyst = (
        summary.get("weak_catalyst_dynamic")
        if isinstance(summary.get("weak_catalyst_dynamic"), Mapping)
        else {}
    )
    weak_catalyst_review = (
        summary.get("dynamic_weak_catalyst_review")
        if isinstance(summary.get("dynamic_weak_catalyst_review"), Mapping)
        else {}
    )
    risk_guards = (
        summary.get("risk_guards") if isinstance(summary.get("risk_guards"), Mapping) else {}
    )

    lines = [
        f"Daily Summary {summary.get('date', '')} [{summary.get('user_id', 'default')}]",
        (
            "PnL: "
            f"realized={_fmt_money(overall.get('realized'))} "
            f"unrealized={_fmt_money(overall.get('unrealized'))} "
            f"total={_fmt_money(overall.get('total'))}"
        ),
        (
            "Postmortem: "
            f"trades={int(_safe_float(metrics.get('trade_count')))} "
            f"win_rate={_fmt_pct(metrics.get('win_rate_pct'))} "
            f"avg_win={_fmt_money(metrics.get('average_winner'))} "
            f"avg_loss={_fmt_money(metrics.get('average_loser'))} "
            f"pf={_fmt_pf(metrics.get('profit_factor'))}"
        ),
        (
            "Unrealized source: "
            f"{broker_open.get('source') or 'trade_attribution_only'} "
            f"broker_open_unrealized={_fmt_money(broker_open.get('unrealized'))} "
            f"positions={int(_safe_float(broker_open.get('positions')))}"
        ),
        (
            "Activity: "
            f"submitted_orders={int(_safe_float(order_activity.get('submitted_orders')))} "
            f"buys={int(_safe_float(order_activity.get('buy_orders')))} "
            f"sells={int(_safe_float(order_activity.get('sell_orders')))} "
            f"exits={int(_safe_float(order_activity.get('exit_records')))} "
            f"pnl_missing_exits={int(_safe_float(order_activity.get('pnl_missing_exits')))} "
            f"symbols={','.join(order_activity.get('symbols') or []) or 'none'}"
        ),
        (
            "Activity sources: "
            f"attribution_orders={int(_safe_float(count_sources.get('attribution_orders')))} "
            f"attribution_exits={int(_safe_float(count_sources.get('attribution_exits')))} "
            f"order_history_orders={int(_safe_float(count_sources.get('order_history_orders')))} "
            f"live_log_orders={int(_safe_float(count_sources.get('live_log_orders')))} "
            f"live_log_exits={int(_safe_float(count_sources.get('live_log_exits')))} "
            f"broker_fill_logs={int(_safe_float(count_sources.get('broker_fill_logs')))} "
            f"local_log_lines={int(_safe_float(count_sources.get('local_log_lines')))} "
            f"journal_log_lines={int(_safe_float(count_sources.get('journal_log_lines')))}"
        ),
        (
            "MFE/MAE: "
            f"avg_mfe={_fmt_pct(mfe_mae.get('avg_mfe_pct'))} "
            f"avg_mae={_fmt_pct(mfe_mae.get('avg_mae_pct'))} "
            f"count={int(_safe_float(mfe_mae.get('count')))}"
        ),
        "Attribution: " + ("; ".join(_top_routes(profitability)) or "none"),
        (
            "Churn: "
            f"reversals={int(_safe_float(reversals.get('count')))} "
            f"repeat_buys={int(_safe_float(repeats.get('repeated_buy_count')))} "
            f"repeat_sells={int(_safe_float(repeats.get('repeated_sell_count')))} "
            f"weak_exits={int(_safe_float(weak.get('count')))}"
        ),
    ]
    if catalyst_stats:
        lines.append("Catalysts: " + "; ".join(_top_catalysts(catalyst_stats)))
    else:
        lines.append("Catalysts: no catalyst outcomes recorded.")
    lines.extend(_replay_summary_lines(summary.get("replay") if isinstance(summary.get("replay"), Mapping) else None))

    profitable_routes = [
        route
        for route, stats in sorted(route_stats.items())
        if isinstance(stats, Mapping) and int(stats.get("trades") or 0) > 0
    ]
    if profitable_routes:
        lines.append(
            "Route stats: "
            + "; ".join(
                f"{route} trades={int(route_stats[route].get('trades') or 0)} "
                f"win={_fmt_pct(route_stats[route].get('win_rate'), fraction=True)} "
                f"pf={_fmt_pf(route_stats[route].get('profit_factor'))}"
                for route in profitable_routes[:4]
            )
        )
    if weak_catalyst:
        lines.append(
            "Weak catalyst dynamic: "
            f"trades={int(_safe_float(weak_catalyst.get('trades')))} "
            f"pnl={_fmt_money(weak_catalyst.get('pnl'))} "
            f"win={_fmt_pct(weak_catalyst.get('win_rate'), fraction=True)}"
        )
    if weak_catalyst_review:
        lines.append(
            "Dynamic weak catalyst review: "
            f"classified={int(_safe_float(weak_catalyst_review.get('classified')))} "
            f"rejected={int(_safe_float(weak_catalyst_review.get('rejected')))} "
            f"size_reduced={int(_safe_float(weak_catalyst_review.get('size_reduced')))} "
            f"orders={int(_safe_float(weak_catalyst_review.get('orders')))} "
            f"pnl={_fmt_money(weak_catalyst_review.get('realized_pnl'))} "
            f"recommendation={weak_catalyst_review.get('recommendation') or 'leave unchanged'}"
        )
    if risk_guards:
        guards = risk_guards.get("triggered_guards") if isinstance(risk_guards.get("triggered_guards"), list) else []
        lines.append(
            "Risk guards: "
            f"triggered={','.join(str(item) for item in guards) or 'none'} "
            f"trend_long_blocked={str(bool(risk_guards.get('trend_long_entries_blocked'))).lower()} "
            f"new_entries_blocked={str(bool(risk_guards.get('new_entries_blocked'))).lower()} "
            f"flatten_risk={str(bool(risk_guards.get('flatten_risk'))).lower()} "
            f"loss_pct_equity={_safe_float(risk_guards.get('loss_pct_equity'), 0.0):.4f}"
        )
    if top_winners:
        lines.append(
            "Top winners: "
            + "; ".join(
                f"{row.get('symbol', 'UNKNOWN')} {row.get('route', 'unknown')} {_fmt_money(row.get('pnl'))}"
                for row in top_winners[:3]
                if isinstance(row, Mapping)
            )
        )
    if top_losers:
        lines.append(
            "Top losers: "
            + "; ".join(
                f"{row.get('symbol', 'UNKNOWN')} {row.get('route', 'unknown')} {_fmt_money(row.get('pnl'))}"
                for row in top_losers[:3]
                if isinstance(row, Mapping)
            )
        )
    suggestions = getattr(postmortem, "suggestions", []) or []
    if suggestions:
        lines.append("Review: " + " ".join(str(item) for item in suggestions[:2]))
    return "\n".join(lines)


__all__ = [
    "build_combined_daily_summary",
    "format_combined_daily_summary",
]
