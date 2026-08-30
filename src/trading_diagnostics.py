"""Reconciliation, expectancy, news edge, and experiment diagnostics."""
from __future__ import annotations

import argparse
import grp
import hashlib
import json
import math
import os
import pwd
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from src.config_loader import load_config
from src.artifact_writability import artifact_file_readable_by_runtime, artifact_target_diagnostics, atomic_write_text
from src.options_premium_risk import is_option_symbol
from src.options_selector import parse_occ_equity_option_symbol
from src.runtime_progress import load_runtime_progress, summarize_session_activity
from src.trade_attribution import attribution_daily_path, load_daily_artifact, record_recovered_order_event
from src.trading_control import is_expected_entry_block, resolve_trading_mode, strategy_states
from src.trading_lifecycle import (
    build_canonical_day,
    canonical_fill_key as lifecycle_canonical_fill_key,
    canonical_order_state,
    event_trading_date_et,
    is_replay_or_mock_row,
    is_shadow_row as lifecycle_is_shadow_row,
    lifecycle_record_class,
    lifecycle_status,
    quarantine_candidates,
    real_filled_row,
    real_submitted_row,
    shadow_reclassification_reason,
)

REPORT_ROOT = Path(os.environ.get("ALGO_REPORT_ROOT", str(Path(__file__).resolve().parent.parent / "reports")))
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

LOSS_PRIMARY_CAUSES = {
    "BAD_SIGNAL",
    "LATE_ENTRY",
    "CHASED_ENTRY",
    "BAD_OPTION_CONTRACT",
    "WIDE_SPREAD",
    "LOW_LIQUIDITY",
    "IV_COLLAPSE",
    "SLIPPAGE",
    "EXIT_GIVEBACK",
    "STOP_TOO_WIDE",
    "STOP_TOO_TIGHT",
    "MARKET_REGIME_FAILURE",
    "DATA_STALE",
    "MISSING_FEATURE",
    "RUNTIME_FAILURE",
    "BROKER_EXECUTION_FAILURE",
    "UNRECONCILED",
    "INSUFFICIENT_DATA",
}


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _safe_int(value: Any) -> int:
    f = _safe_float(value)
    return int(f) if f is not None else 0


def _norm_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _norm_id(value: Any) -> str:
    return str(value or "").strip()


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def event_timestamp(row: Mapping[str, Any]) -> datetime | None:
    """Return the broker event timestamp, falling back to persisted timestamp only."""

    for key in (
        "event_timestamp_utc",
        "broker_event_timestamp",
        "broker_timestamp",
        "transaction_time",
        "filled_at",
        "submitted_at",
        "created_at",
        "timestamp",
    ):
        ts = _parse_ts(row.get(key))
        if ts is not None:
            return ts.astimezone(timezone.utc)
    return None


def event_trading_date_et(row: Mapping[str, Any]) -> str | None:
    ts = event_timestamp(row)
    return ts.astimezone(ET).date().isoformat() if ts is not None else None


def normalize_lifecycle_timestamps(row: Mapping[str, Any]) -> dict[str, Any]:
    ts = event_timestamp(row)
    return {
        "event_timestamp_utc": ts.isoformat() if ts is not None else None,
        "event_trading_date_et": ts.astimezone(ET).date().isoformat() if ts is not None else None,
        "ingested_at_utc": (_parse_ts(row.get("ingested_at_utc") or row.get("ingested_at")) or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
    }


def _date_range(start: str, end: str) -> list[str]:
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    if d1 < d0:
        raise ValueError("--to must be on or after --from")
    days: list[str] = []
    cur = d0
    while cur <= d1:
        days.append(cur.isoformat())
        cur += timedelta(days=1)
    return days


def atomic_write(path: Path, content: str) -> None:
    atomic_write_text(path, content, generator="trading_diagnostics")


def _load_daily(root: Path, day: str, user: str) -> dict[str, Any]:
    return load_daily_artifact(attribution_daily_path(data_dir=root / "data", user_id=user, day=day))


def _rows(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    return [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _row_status(row: Mapping[str, Any]) -> str:
    text = str(row.get("status") or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _normalize_broker_enum(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _row_side(row: Mapping[str, Any]) -> str:
    return str(row.get("action") or row.get("side") or "").strip().lower()


def _obj_value(obj: Any, *keys: str) -> Any:
    if isinstance(obj, Mapping):
        for key in keys:
            if key in obj:
                return obj.get(key)
        return None
    for key in keys:
        value = getattr(obj, key, None)
        if value is not None:
            return value
    return None


def _position_qty(row: Any) -> float:
    return abs(_safe_float(_obj_value(row, "qty", "quantity")) or 0.0)


def _position_notional(row: Any) -> float:
    value = abs(_safe_float(_obj_value(row, "market_value", "notional", "current_value")) or 0.0)
    if value > 0.0:
        return value
    qty = _position_qty(row)
    price = abs(_safe_float(_obj_value(row, "current_price", "price", "market_price")) or 0.0)
    return qty * price


def _is_synthetic_or_replay(row: Mapping[str, Any]) -> bool:
    return is_replay_or_mock_row(row)


def _is_shadow_row(row: Mapping[str, Any]) -> bool:
    return lifecycle_is_shadow_row(row)


def _is_live_mixed(row: Mapping[str, Any], user: str) -> bool:
    mode = str(row.get("mode") or row.get("environment") or "").strip().lower()
    return user == "live_bot" and mode in {"paper", "replay", "mock"}


def _submitted_row(row: Mapping[str, Any]) -> bool:
    return bool(row.get("submitted")) or bool(row.get("submit_attempt"))


def _real_submitted_row(row: Mapping[str, Any]) -> bool:
    return real_submitted_row(row)


def _broker_accepted_row(row: Mapping[str, Any]) -> bool:
    status = _row_status(row)
    return status in {"new", "accepted", "pending_new", "partially_filled", "filled", "done_for_day", "calculated"}


def _filled_row(row: Mapping[str, Any]) -> bool:
    return real_filled_row(row)


def _complete_fill_row(row: Mapping[str, Any]) -> bool:
    qty = _safe_float(row.get("qty") or row.get("quantity")) or 0.0
    filled = _safe_float(row.get("filled_qty")) or 0.0
    return _row_status(row) == "filled" or (qty > 0 and filled >= qty)


def canonical_order_key(row: Mapping[str, Any]) -> str:
    for label, key in (("broker_order_id", "broker_order_id"), ("broker_order_id", "order_id"), ("client_order_id", "client_order_id"), ("logical_order_id", "logical_order_id")):
        value = _norm_id(row.get(key))
        if value:
            return f"{label}:{value}"
    ts = (event_timestamp(row) or _parse_ts(row.get("timestamp")))
    ts_s = ts.astimezone(timezone.utc).isoformat() if ts else ""
    payload = {
        "symbol": _norm_symbol(row.get("option_symbol") or row.get("symbol")),
        "side": _row_side(row),
        "timestamp": ts_s,
        "notional": _safe_float(row.get("notional")),
        "qty": _safe_float(row.get("qty") or row.get("quantity")),
        "route": row.get("route"),
        "source": row.get("source"),
    }
    return "submission_attempt:" + hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def _explicit_fill_id(row: Mapping[str, Any]) -> str:
    for key in ("broker_activity_id", "activity_id", "broker_fill_id", "fill_id", "execution_id"):
        value = _norm_id(row.get(key))
        if value:
            return value
    return ""


def canonical_position_key(row: Mapping[str, Any], *, user: str) -> str:
    symbol = _norm_symbol(row.get("option_symbol") or row.get("symbol"))
    parsed = parse_occ_equity_option_symbol(symbol)
    if parsed:
        underlying, exp, right, strike = parsed
        return f"option:{user}:{underlying}:{exp.isoformat()}:{strike}:{right}:{symbol}"
    return f"equity:{user}:{symbol}"


@dataclass(frozen=True)
class SourceRow:
    source: str
    path: str
    index: int
    row: dict[str, Any]


@dataclass(frozen=True)
class FillEvent:
    key: str
    order_key: str
    position_key: str
    side: str
    quantity: float
    price: float | None
    timestamp: datetime | None
    source_rows: tuple[SourceRow, ...]
    classification: str = "EXACT_MATCH"


def lifecycle_sources(*, root: Path, day: str, user: str) -> dict[str, Any]:
    attr_path = attribution_daily_path(data_dir=root / "data", user_id=user, day=day)
    return {
        "entry_decisions": {"authoritative": "trade_attribution.candidates", "path": str(attr_path)},
        "submitted_orders": {"authoritative": "trade_attribution.orders unique canonical_order_key", "path": str(attr_path)},
        "broker_acknowledgements": {"authoritative": "trade_attribution.orders status snapshots, de-duplicated by canonical_order_key", "path": str(attr_path)},
        "order_snapshots": {"authoritative": "trade_attribution.orders raw rows, not trade count", "path": str(attr_path)},
        "fill_events": {"authoritative": "broker fill/activity id when present; otherwise cumulative order fill deltas", "path": str(attr_path)},
        "positions": {"authoritative": "derived from unique fill events by canonical_position_key", "path": str(attr_path)},
        "exit_orders": {"authoritative": "unique sell fill/order lineage", "path": str(attr_path)},
        "closed_positions": {"authoritative": "unique exit fills matched to canonical positions", "path": str(attr_path)},
        "trade_attribution": {"authoritative": "research sidecar; raw rows are not unique broker events", "path": str(attr_path)},
        "daily_aggregates": {"authoritative": "not authoritative for lifecycle counts", "path": str(root / "data")},
    }


def _source_rows(rows: Sequence[Mapping[str, Any]], *, path: Path, source: str) -> list[SourceRow]:
    return [SourceRow(source=source, path=str(path), index=i, row=dict(row)) for i, row in enumerate(rows)]


def _rows_for_et_day(rows: Sequence[Mapping[str, Any]], *, day: str) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if event_trading_date_et(row) == day]


def _unique_orders(source_rows: Sequence[SourceRow]) -> dict[str, list[SourceRow]]:
    out: dict[str, list[SourceRow]] = defaultdict(list)
    for src in source_rows:
        out[canonical_order_key(src.row)].append(src)
    return dict(out)


def _order_snapshot_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        canonical_order_key(row),
        _row_status(row),
        _safe_float(row.get("filled_qty")),
        _safe_float(row.get("filled_avg_price") or row.get("fill_price") or row.get("price")),
        str(row.get("event_origin") or ""),
        str(row.get("recovery_key") or ""),
    )


def _duplicate_order_snapshots(source_rows: Sequence[SourceRow]) -> tuple[list[SourceRow], list[str]]:
    grouped: dict[tuple[Any, ...], list[SourceRow]] = defaultdict(list)
    for src in source_rows:
        grouped[_order_snapshot_signature(src.row)].append(src)
    duplicate_sources = [src for rows in grouped.values() for src in rows[1:]]
    duplicate_ids = sorted({str(sig[0]) for sig, rows in grouped.items() if len(rows) > 1})
    return duplicate_sources, duplicate_ids


def _raw_fill_sources(source_rows: Sequence[SourceRow]) -> list[SourceRow]:
    return [src for src in source_rows if _filled_row(src.row)]


def _canonical_fill_events(source_rows: Sequence[SourceRow], *, user: str) -> tuple[list[FillEvent], list[SourceRow]]:
    submitted_order_keys = {
        canonical_order_key(src.row)
        for src in source_rows
        if _real_submitted_row(src.row)
    }
    by_order = _unique_orders(_raw_fill_sources(source_rows))
    events: list[FillEvent] = []
    duplicate_sources: list[SourceRow] = []
    for order_key, group in sorted(by_order.items()):
        if order_key not in submitted_order_keys or any(_is_synthetic_or_replay(src.row) or _is_shadow_row(src.row) for src in group):
            duplicate_sources.extend(group[1:])
            continue
        explicit: dict[str, list[SourceRow]] = defaultdict(list)
        implicit: list[SourceRow] = []
        for src in group:
            fid = lifecycle_canonical_fill_key(src.row) if _explicit_fill_id(src.row) else ""
            if fid:
                explicit[fid].append(src)
            else:
                implicit.append(src)
        for fill_key, rows in explicit.items():
            first = rows[0].row
            duplicate_sources.extend(rows[1:])
            events.append(
                FillEvent(
                    key=fill_key,
                    order_key=order_key,
                    position_key=canonical_position_key(first, user=user),
                    side=_row_side(first),
                    quantity=_safe_float(first.get("filled_qty") or first.get("qty") or first.get("quantity")) or 0.0,
                    price=_safe_float(first.get("filled_avg_price") or first.get("fill_price") or first.get("price")),
                    timestamp=event_timestamp(first),
                    source_rows=tuple(rows),
                )
            )
        if not implicit:
            continue
        implicit_sorted = sorted(implicit, key=lambda src: ((event_timestamp(src.row) or datetime.min.replace(tzinfo=timezone.utc)), src.index))
        seen_snapshots: set[tuple[float, float | None, str]] = set()
        prev_qty = 0.0
        for src in implicit_sorted:
            row = src.row
            cumulative_qty = _safe_float(row.get("filled_qty") or row.get("qty") or row.get("quantity")) or 0.0
            price = _safe_float(row.get("filled_avg_price") or row.get("fill_price") or row.get("price"))
            ts = event_timestamp(row)
            snapshot_key = (cumulative_qty, price, _row_status(row))
            if snapshot_key in seen_snapshots and cumulative_qty <= prev_qty:
                duplicate_sources.append(src)
                continue
            seen_snapshots.add(snapshot_key)
            delta = cumulative_qty - prev_qty
            if delta <= 0:
                duplicate_sources.append(src)
                continue
            prev_qty = cumulative_qty
            ts_s = ts.isoformat() if ts else ""
            fill_key = f"composite:{order_key}:{_norm_symbol(row.get('option_symbol') or row.get('symbol'))}:{_row_side(row)}:{ts_s}:{delta:g}:{price}"
            events.append(
                FillEvent(
                    key=fill_key,
                    order_key=order_key,
                    position_key=canonical_position_key(row, user=user),
                    side=_row_side(row),
                    quantity=delta,
                    price=price,
                    timestamp=ts,
                    source_rows=(src,),
                    classification="DETERMINISTIC_RECOVERY",
                )
            )
    return events, duplicate_sources


def _position_state(fill_events: Sequence[FillEvent]) -> dict[str, Any]:
    opened: dict[str, dict[str, Any]] = {}
    closed: dict[str, dict[str, Any]] = {}
    for event in fill_events:
        book = opened.setdefault(
            event.position_key,
            {
                "position_id": event.position_key,
                "entry_fill_ids": [],
                "exit_fill_ids": [],
                "entry_qty": 0.0,
                "exit_qty": 0.0,
                "status": "open",
            },
        )
        if event.side == "sell":
            book["exit_qty"] += event.quantity
            book["exit_fill_ids"].append(event.key)
        else:
            book["entry_qty"] += event.quantity
            book["entry_fill_ids"].append(event.key)
            closed.pop(event.position_key, None)
        if book["entry_qty"] > 0 and book["exit_qty"] >= book["entry_qty"]:
            book["status"] = "closed"
            closed[event.position_key] = book
        elif book["exit_qty"] > 0:
            book["status"] = "partially_closed"
    return {"positions": opened, "closed": closed, "open": {k: v for k, v in opened.items() if v.get("status") != "closed"}}


def build_lifecycle_state(*, root: Path, day: str, user: str) -> dict[str, Any]:
    payload = _load_daily(root, day, user)
    attr_path = attribution_daily_path(data_dir=root / "data", user_id=user, day=day)
    raw_candidates_all = _rows(payload, "candidates")
    raw_allocator_all = _rows(payload, "allocator_candidates")
    raw_orders_all = _rows(payload, "orders")
    raw_exits_all = _rows(payload, "exits")
    candidates = _rows_for_et_day(raw_candidates_all, day=day)
    allocator = _rows_for_et_day(raw_allocator_all, day=day)
    orders = _rows_for_et_day(raw_orders_all, day=day)
    exits = _rows_for_et_day(raw_exits_all, day=day)
    excluded_wrong_date = (
        len(raw_candidates_all) - len(candidates)
        + len(raw_allocator_all) - len(allocator)
        + len(raw_orders_all) - len(orders)
        + len(raw_exits_all) - len(exits)
    )
    order_sources = _source_rows(orders, path=attr_path, source="trade_attribution.orders")
    shadow_order_sources = [src for src in order_sources if _is_shadow_row(src.row)]
    raw_submitted_sources = [src for src in order_sources if _real_submitted_row(src.row)]
    clean_order_sources = [src for src in order_sources if not _is_synthetic_or_replay(src.row) and not _is_shadow_row(src.row)]
    submitted_sources = [src for src in clean_order_sources if _real_submitted_row(src.row)]
    unique_submitted = _unique_orders(submitted_sources)
    raw_fills = _raw_fill_sources(order_sources)
    synthetic = [src for src in order_sources if _is_synthetic_or_replay(src.row)]
    ambiguous_sources = [src for src in order_sources if lifecycle_record_class(src.row) == "AMBIGUOUS_MALFORMED"]
    snapshot_sources = [src for src in order_sources if lifecycle_record_class(src.row) == "ATTRIBUTION_SNAPSHOT"]
    clean_submitted_keys = set(unique_submitted)
    orphan_fill_sources = [
        src
        for src in _raw_fill_sources(clean_order_sources)
        if canonical_order_key(src.row) not in clean_submitted_keys
    ]
    fills, duplicate_fill_sources = _canonical_fill_events(order_sources, user=user)
    position_state = _position_state(fills)
    raw_submitted_by_order = _unique_orders([src for src in order_sources if _real_submitted_row(src.row)])
    duplicate_order_sources, duplicate_order_ids = _duplicate_order_snapshots([src for src in order_sources if _real_submitted_row(src.row)])
    shadow_by_order = _unique_orders(shadow_order_sources)
    duplicate_shadow_sources = [src for group in shadow_by_order.values() for src in group[1:]]
    duplicate_shadow_ids = sorted(key for key, group in shadow_by_order.items() if len(group) > 1)
    duplicate_fill_ids = sorted({src.row.get("order_id") or canonical_order_key(src.row) for src in duplicate_fill_sources})
    broker_accepted = [src for src in submitted_sources if _broker_accepted_row(src.row)]
    latest_by_order: dict[str, SourceRow] = {}
    for key, group in unique_submitted.items():
        latest_by_order[key] = sorted(
            group,
            key=lambda src: ((event_timestamp(src.row) or datetime.min.replace(tzinfo=timezone.utc)), src.index),
        )[-1]
    canonical_order_states = {key: canonical_order_state(src.row) for key, src in latest_by_order.items()}
    broker_confirmed_order_keys = {key for key, state_name in canonical_order_states.items() if state_name != "UNKNOWN_BROKER_STATE"}
    broker_terminal_order_keys = {
        key
        for key, state_name in canonical_order_states.items()
        if state_name in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "REPLACED"}
    }
    broker_current_order_keys = set(canonical_order_states) - broker_terminal_order_keys
    rejected = [src for src in order_sources if _row_status(src.row) == "rejected" or src.row.get("reject_reason")]
    cancelled = [src for src in order_sources if _row_status(src.row) in {"canceled", "cancelled"}]
    partial = [src for src in order_sources if _row_status(src.row) == "partially_filled"]
    completed = [event for event in fills if event.side != "sell"]
    exit_fill_events = [event for event in fills if event.side == "sell"]
    recovered_order_sources = [src for src in order_sources if src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True]
    recovered_fill_sources = [src for src in raw_fills if src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True]
    recovered_fill_keys = {
        event.key
        for event in fills
        if any(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in event.source_rows)
    }
    position_lineage = {}
    for pos_id, pos in position_state["positions"].items():
        entry_fill_ids = set(pos.get("entry_fill_ids") or [])
        source_rows_for_position = [
            src
            for event in fills
            if event.position_key == pos_id and event.key in entry_fill_ids
            for src in event.source_rows
        ]
        if source_rows_for_position and all(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in source_rows_for_position):
            classification = "RECOVERED_LINEAGE"
        elif source_rows_for_position:
            classification = "EXACT_LINEAGE"
        else:
            classification = "UNKNOWN_POSITION"
        position_lineage[pos_id] = {**pos, "lineage_classification": classification}
    counts = {
        "scanner_events": len(candidates),
        "selected_candidates": len([r for r in allocator if r.get("action_created") or r.get("selected_rank") is not None]),
        "entry_evaluations": len(candidates),
        "approved_entries": len([r for r in candidates if r.get("accepted") is True]),
        "blocked_entries": len([r for r in candidates if r.get("accepted") is False]),
        "raw_submitted_order_events": len(raw_submitted_sources),
        "unique_submitted_orders": len(unique_submitted),
        "raw_local_order_snapshots": len([src for src in order_sources if not (src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True)]),
        "recovered_broker_order_snapshots": len(recovered_order_sources),
        "raw_broker_accepted_order_events": len(broker_accepted),
        "unique_broker_accepted_orders": len(_unique_orders(broker_accepted)),
        "local_submitted_orders": len(unique_submitted),
        "broker_confirmed_orders": len(broker_confirmed_order_keys),
        "broker_current_orders": len(broker_current_order_keys),
        "broker_terminal_orders": len(broker_terminal_order_keys),
        "broker_filled_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "FILLED"),
        "broker_partially_filled_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "PARTIALLY_FILLED"),
        "broker_canonical_accepted_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "ACCEPTED"),
        "broker_rejected_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "REJECTED"),
        "broker_cancelled_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "CANCELLED"),
        "broker_unresolved_orders": sum(1 for state_name in canonical_order_states.values() if state_name in {"PENDING", "UNKNOWN_BROKER_STATE"}),
        "broker_pending_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "PENDING"),
        "broker_unknown_state_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "UNKNOWN_BROKER_STATE"),
        "broker_expired_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "EXPIRED"),
        "broker_replaced_orders": sum(1 for state_name in canonical_order_states.values() if state_name == "REPLACED"),
        "broker_accepted_orders_local": len(_unique_orders([src for src in broker_accepted if src.row.get("event_origin") != "broker_reconciliation" and src.row.get("recovered") is not True])),
        "broker_accepted_orders_reconciled": len(_unique_orders([src for src in broker_accepted if src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True])),
        "rejected_orders": len(rejected),
        "cancelled_orders": len(cancelled),
        "raw_fill_events": len(raw_fills),
        "local_fill_events": len([src for src in raw_fills if src.row.get("event_origin") != "broker_reconciliation" and src.row.get("recovered") is not True]),
        "raw_local_fill_events": len([src for src in raw_fills if src.row.get("event_origin") != "broker_reconciliation" and src.row.get("recovered") is not True]),
        "recovered_broker_fill_events": len(recovered_fill_sources),
        "contaminated_fill_events": len(ambiguous_sources),
        "orphan_fill_events": len(orphan_fill_sources),
        "unique_fills": len(fills),
        "canonical_fills": len(fills),
        "broker_reconciled_fill_events": len(recovered_fill_keys),
        "duplicate_fill_events": len(duplicate_fill_sources),
        "duplicate_order_events": len(duplicate_order_sources),
        "duplicate_shadow_intent_events": len(duplicate_shadow_sources),
        "partial_fill_events": len(partial),
        "replay_event_count": 0,
        "raw_position_records": len(raw_fills),
        "unique_opened_positions": len(position_state["positions"]),
        "canonical_positions": len(position_state["positions"]),
        "positions_with_exact_lineage": len([p for p in position_lineage.values() if p.get("lineage_classification") == "EXACT_LINEAGE"]),
        "positions_with_recovered_lineage": len([p for p in position_lineage.values() if p.get("lineage_classification") == "RECOVERED_LINEAGE"]),
        "positions_with_missing_lineage": len([p for p in position_lineage.values() if p.get("lineage_classification") in {"AMBIGUOUS_LINEAGE", "UNKNOWN_POSITION"}]),
        "local_positions_today": len(
            {
                event.position_key
                for event in fills
                if not any(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in event.source_rows)
            }
        ),
        "broker_reconciled_positions_today": len(
            {
                event.position_key
                for event in fills
                if any(src.row.get("event_origin") == "broker_reconciliation" or src.row.get("recovered") is True for src in event.source_rows)
            }
        ),
        "unique_closed_positions": len(position_state["closed"]),
        "unique_still_open_positions": len(position_state["open"]),
        "exit_orders": len([src for src in order_sources if _row_side(src.row) == "sell"]),
        "unique_exit_fills": len(exit_fill_events),
        "synthetic_or_replay_order_events": len(ambiguous_sources),
        "unresolved_contamination": len(ambiguous_sources),
        "attribution_snapshots": len(snapshot_sources),
        "duplicate_attribution_snapshots": sum(max(0, len(group) - 1) for group in _unique_orders(snapshot_sources).values()),
        "replay_research_outcomes": len(synthetic),
        "duplicate_replay_research_outcomes": sum(max(0, len(group) - 1) for group in _unique_orders(synthetic).values()),
        "shadow_hypothetical_outcomes": len(shadow_order_sources),
        "ambiguous_unresolved_records": len(ambiguous_sources),
        "real_broker_fills": len(fills),
        "real_broker_positions": len(position_state["positions"]),
        "legacy_shadow_records_reclassified": len(
            [src for src in shadow_order_sources if str(shadow_reclassification_reason(src.row) or "").startswith("legacy_")]
        ),
        "real_order_submission_attempts": len(raw_submitted_sources),
        "real_broker_accepted_orders": len(_unique_orders(broker_accepted)),
        "shadow_decisions": len([r for r in candidates if _is_shadow_row(r)]),
        "shadow_allocator_actions": len(
            [
                r
                for r in allocator
                if _is_shadow_row(r) and (r.get("action_created") or r.get("selected_rank") is not None)
            ]
        ),
        "shadow_order_intents": len([src for src in shadow_order_sources if _submitted_row(src.row) or src.row.get("hypothetical")]),
        "shadow_execution_blocks": len(
            [
                src
                for src in shadow_order_sources
                if src.row.get("broker_dispatch_attempted") is False
                or src.row.get("execution_allowed") is False
                or "blocked" in str(src.row.get("reason") or src.row.get("status") or "").lower()
            ]
        ),
        "excluded_wrong_or_missing_event_date_records": excluded_wrong_date,
        # Backward-compatible names now point at canonical counts.
        "submitted_orders": len(unique_submitted),
        "broker_accepted_orders": len(_unique_orders(broker_accepted)),
        "completed_fills": len(completed),
        "opened_positions": len(position_state["positions"]),
        "closed_positions": len(position_state["closed"]),
        "still_open_positions": len(position_state["open"]),
    }
    return {
        "payload": payload,
        "path": attr_path,
        "candidates": candidates,
        "allocator": allocator,
        "orders": orders,
        "exits": exits,
        "order_sources": order_sources,
        "shadow_order_sources": shadow_order_sources,
        "raw_submitted_sources": raw_submitted_sources,
        "submitted_sources": submitted_sources,
        "unique_submitted": unique_submitted,
        "raw_fills": raw_fills,
        "fill_events": fills,
        "duplicate_order_sources": duplicate_order_sources,
        "duplicate_shadow_sources": duplicate_shadow_sources,
        "duplicate_fill_sources": duplicate_fill_sources,
        "duplicate_order_ids": duplicate_order_ids,
        "duplicate_shadow_ids": duplicate_shadow_ids,
        "duplicate_fill_ids": duplicate_fill_ids,
        "position_state": position_state,
        "position_lineage": position_lineage,
        "order_reconciliation": {
            key: {
                "canonical_order_key": key,
                "decision_id": latest_by_order[key].row.get("decision_id"),
                "logical_order_id": latest_by_order[key].row.get("logical_order_id") or latest_by_order[key].row.get("order_id"),
                "client_order_id": latest_by_order[key].row.get("client_order_id"),
                "route": latest_by_order[key].row.get("route") or latest_by_order[key].row.get("source") or latest_by_order[key].row.get("strategy"),
                "submitted_at": latest_by_order[key].row.get("submitted_at") or latest_by_order[key].row.get("created_at") or latest_by_order[key].row.get("timestamp"),
                "terminal_state": state_name,
                "broker_status": _row_status(latest_by_order[key].row),
                "broker_status_timestamp": latest_by_order[key].row.get("broker_status_timestamp") or latest_by_order[key].row.get("updated_at") or latest_by_order[key].row.get("timestamp"),
                "reconciled_at": latest_by_order[key].row.get("reconciled_at") or latest_by_order[key].row.get("reconciliation_timestamp"),
                "broker_order_id": latest_by_order[key].row.get("broker_order_id") or latest_by_order[key].row.get("order_id"),
                "symbol": latest_by_order[key].row.get("symbol"),
                "side": _row_side(latest_by_order[key].row),
            }
            for key, state_name in sorted(canonical_order_states.items())
        },
        "counts": counts,
        "sources": lifecycle_sources(root=root, day=day, user=user),
    }


@dataclass(frozen=True)
class ReconciliationResult:
    report: dict[str, Any]
    problems: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return not self.problems


def calculate_spread_pct(*, bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    return ((ask - bid) / mid) * 100.0 if mid > 0 else None


def calculate_slippage_from_mid(*, fill_price: float | None, bid: float | None, ask: float | None, side: str = "buy") -> float | None:
    if fill_price is None or bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    return (fill_price - mid) if side.lower() == "buy" else (mid - fill_price)


def calculate_mfe_mae(prices: Sequence[float], *, entry_price: float, side: str = "long") -> dict[str, Any]:
    if entry_price <= 0 or not prices:
        return {"mfe_pct": None, "mae_pct": None, "time_to_mfe_index": None, "time_to_mae_index": None}
    returns = [((p - entry_price) / entry_price) * 100.0 for p in prices]
    if side.lower() in {"short", "put"}:
        returns = [-r for r in returns]
    mfe = max(returns)
    mae = min(returns)
    return {
        "mfe_pct": mfe,
        "mae_pct": mae,
        "time_to_mfe_index": returns.index(mfe),
        "time_to_mae_index": returns.index(mae),
    }


def mfe_capture_ratio(*, realized_profit: float | None, mfe: float | None) -> float | None:
    if realized_profit is None or mfe is None or mfe <= 0:
        return None
    return realized_profit / mfe


def classify_loss(trade: Mapping[str, Any], *, reconciled: bool = True) -> dict[str, Any]:
    """Deterministic primary loss attribution with evidence."""
    evidence: dict[str, Any] = {}
    if not reconciled:
        return {"primary": "UNRECONCILED", "contributing": [], "evidence": evidence}
    pnl = _safe_float(trade.get("realized_pnl", trade.get("pnl")))
    if pnl is None:
        return {"primary": "INSUFFICIENT_DATA", "contributing": [], "evidence": {"missing": "realized_pnl"}}
    if pnl >= 0:
        return {"primary": "INSUFFICIENT_DATA", "contributing": [], "evidence": {"not_losing_trade": True}}
    fwd = _safe_float(trade.get("underlying_forward_return_15m", trade.get("forward_return_15m")))
    mfe = _safe_float(trade.get("mfe_pct", trade.get("max_favorable_excursion_pct")))
    capture = mfe_capture_ratio(realized_profit=pnl, mfe=mfe)
    spread = _safe_float(trade.get("spread_pct"))
    slippage = _safe_float(trade.get("slippage_from_mid", trade.get("slippage_cost")))
    option_response = _safe_float(trade.get("option_return_pct"))
    underlying_return = _safe_float(trade.get("underlying_return_pct"))
    runtime_error = trade.get("runtime_error") or trade.get("exception")
    contributing: list[str] = []
    if runtime_error:
        return {"primary": "RUNTIME_FAILURE", "contributing": [], "evidence": {"runtime_error": str(runtime_error)}}
    spread_threshold = _safe_float(trade.get("wide_spread_threshold_pct"))
    if spread is not None and ((spread_threshold is not None and spread >= spread_threshold) or spread >= 12.0):
        evidence["spread_pct"] = spread
        return {"primary": "WIDE_SPREAD", "contributing": [], "evidence": evidence}
    if slippage is not None and abs(slippage) >= abs(pnl) * 0.25:
        evidence["slippage"] = slippage
        return {"primary": "SLIPPAGE", "contributing": [], "evidence": evidence}
    if mfe is not None and mfe > 0 and capture is not None and capture < 0.25:
        evidence.update({"mfe_pct": mfe, "capture_ratio": capture})
        return {"primary": "EXIT_GIVEBACK", "contributing": [], "evidence": evidence}
    if underlying_return is not None and underlying_return > 0 and (option_response is None or option_response <= 0):
        evidence.update({"underlying_return_pct": underlying_return, "option_return_pct": option_response})
        return {"primary": "BAD_OPTION_CONTRACT", "contributing": [], "evidence": evidence}
    if fwd is not None and fwd <= 0 and (mfe is None or mfe <= 0.5):
        evidence.update({"forward_return_15m": fwd, "mfe_pct": mfe})
        return {"primary": "BAD_SIGNAL", "contributing": [], "evidence": evidence}
    if _safe_float(trade.get("signal_to_fill_seconds")) and _safe_float(trade.get("entered_after_mfe_pct")):
        after = _safe_float(trade.get("entered_after_mfe_pct")) or 0.0
        if after >= 50.0:
            evidence["entered_after_mfe_pct"] = after
            return {"primary": "LATE_ENTRY", "contributing": contributing, "evidence": evidence}
    return {"primary": "INSUFFICIENT_DATA", "contributing": contributing, "evidence": evidence}


def run_trading_audit(*, root: Path = PROJECT_ROOT, day: str, user: str, broker: str | None = None) -> ReconciliationResult:
    state = build_lifecycle_state(root=root, day=day, user=user)
    candidates = state["candidates"]
    orders = state["orders"]
    exits = state["exits"]
    problems: list[dict[str, Any]] = []

    approved = [r for r in candidates if r.get("accepted") is True]
    submitted = [src.row for src in state["submitted_sources"]]
    fills = [src.row for src in state["raw_fills"]]
    fill_events: list[FillEvent] = state["fill_events"]
    duplicate_orders = state["duplicate_order_ids"]
    duplicate_fills = state["duplicate_fill_ids"]
    decision_symbols = {_norm_symbol(r.get("symbol")) for r in approved}
    submitted_order_keys = set(state["unique_submitted"])
    fill_order_keys = {event.order_key for event in fill_events}

    def problem(kind: str, detail: str, **extra: Any) -> None:
        problems.append({"kind": kind, "detail": detail, **extra})

    if int(state["counts"].get("excluded_wrong_or_missing_event_date_records") or 0) > 0:
        problem(
            "incorrect_trading_dates",
            "rows with missing or non-matching ET event dates were excluded from canonical counts",
            record_count=state["counts"].get("excluded_wrong_or_missing_event_date_records"),
        )
    for src in state["submitted_sources"]:
        row = src.row
        if _norm_symbol(row.get("symbol")) not in decision_symbols:
            problem("unmatched_submissions", "submitted order has no approved entry decision", symbol=row.get("symbol"), order_id=row.get("order_id"), canonical_order_key=canonical_order_key(row))
    for event in fill_events:
        if event.order_key not in submitted_order_keys:
            problem("unmatched_fills", "fill has no submitted order", canonical_order_key=event.order_key, fill_id=event.key)
        row = event.source_rows[0].row if event.source_rows else {}
        if _norm_symbol(row.get("symbol")) not in decision_symbols:
            problem("fills_without_decisions", "fill symbol has no approved decision path", symbol=row.get("symbol"), fill_id=event.key)
    for row in exits:
        pos_key = canonical_position_key(row, user=user)
        if pos_key not in state["position_state"]["positions"]:
            problem("exits_without_positions", "exit has no matching canonical position", symbol=row.get("symbol"), position_id=pos_key)
        if not any(k in row for k in ("pnl", "realized_pnl", "realized_pnl_pct", "pnl_pct")):
            problem("missing_exit_records", "exit missing realized P&L fields", symbol=row.get("symbol"))
    if duplicate_orders:
        problem("duplicate_orders", "duplicate raw order snapshots collapsed into canonical orders", order_ids=duplicate_orders)
    if duplicate_fills:
        problem("duplicate_fills", "duplicate raw fill snapshots collapsed into canonical fills", fill_ids=duplicate_fills)
    replay_groups: Counter[str] = Counter()
    for row in orders + exits + candidates:
        row_day = event_trading_date_et(row)
        if row_day and row_day != day:
            problem("incorrect_trading_dates", "broker event trading date differs from audit date", symbol=row.get("symbol"), timestamp=row.get("timestamp"), event_trading_date_et=row_day)
        if _is_live_mixed(row, user):
            problem("paper_records_mixed_into_live", "non-live mode record in live audit", symbol=row.get("symbol"), mode=row.get("mode") or row.get("environment"))
        sym = _norm_symbol(row.get("symbol"))
        if sym and is_option_symbol(sym) and str(row.get("sleeve") or "").lower() in {"stock", "equity"}:
            problem("equity_and_option_mixed", "option symbol marked as equity", symbol=sym)
        if lifecycle_record_class(row) == "AMBIGUOUS_MALFORMED" and user == "live_bot":
            replay_groups[canonical_order_key(row)] += 1
    if replay_groups:
        problem(
            "replay_records_mixed_into_live",
            "synthetic/replay-like records found in live broker lifecycle evidence",
            record_count=sum(replay_groups.values()),
            identity_count=len(replay_groups),
            sample_canonical_order_keys=[{"key": key, "record_count": count} for key, count in sorted(replay_groups.items())[:10]],
        )
    for pos_id, pos in state["position_state"]["positions"].items():
        if not pos.get("entry_fill_ids"):
            problem("positions_without_fills", "canonical position has no entry fill lineage", position_id=pos_id)
    if state["counts"]["unique_opened_positions"] > len([event for event in fill_events if event.side != "sell"]):
        problem("lifecycle_invariant_failed", "unique opened positions exceed unique filled entry orders")
    if state["counts"]["unique_closed_positions"] > state["counts"]["unique_opened_positions"]:
        problem("lifecycle_invariant_failed", "closed positions exceed opened positions")
    expected_open = state["counts"]["unique_opened_positions"] - state["counts"]["unique_closed_positions"]
    if state["counts"]["unique_still_open_positions"] != expected_open:
        problem("lifecycle_invariant_failed", "still open position count mismatch", expected=expected_open, actual=state["counts"]["unique_still_open_positions"])

    realized = sum(_safe_float(r.get("realized_pnl", r.get("pnl"))) or 0.0 for r in exits)
    unrealized = sum(_safe_float(src.row.get("unrealized_pnl")) or 0.0 for src in state["raw_fills"])
    counts = dict(state["counts"])
    counts["realized_pnl"] = realized
    counts["unrealized_pnl"] = unrealized
    submitted_count = int(counts.get("local_submitted_orders") or counts.get("unique_submitted_orders") or 0)
    broker_state_sum = sum(
        int(counts.get(key) or 0)
        for key in (
            "broker_canonical_accepted_orders",
            "broker_partially_filled_orders",
            "broker_filled_orders",
            "broker_cancelled_orders",
            "broker_rejected_orders",
            "broker_expired_orders",
            "broker_replaced_orders",
            "broker_unresolved_orders",
        )
    )
    broker_acceptance_explanation = []
    if submitted_count > int(counts.get("broker_accepted_orders") or 0):
        broker_acceptance_explanation.append(
            "broker_accepted_orders only counts accepted/current accepted-like states; submitted orders may instead be filled, rejected, cancelled, expired, replaced, pending, or unknown."
        )
    if broker_state_sum != submitted_count:
        problem(
            "submitted_order_state_invariant_failed",
            "submitted orders do not sum to exactly one canonical broker state",
            submitted_count=submitted_count,
            broker_state_sum=broker_state_sum,
        )
    canonical_day = build_canonical_day(root=root, day=day, user_id=user)
    integrity = lifecycle_status({**canonical_day["counts"], **counts}, problems)
    report = {
        "date": day,
        "user": user,
        "broker": str(broker or "config").strip().lower(),
        "scope": {
            "name": "canonical_trading_lifecycle",
            "source": str(state["path"]),
            "date_basis": "event_timestamp_america_new_york",
            "environment_filter": "user live_bot excludes replay/mock/test and expected shadow telemetry as broker evidence",
            "record_origin_filter": "broker/application for live broker evidence",
            "raw_count_fields": ["raw_submitted_order_events", "raw_fill_events", "raw_position_records"],
            "unique_count_fields": ["unique_submitted_orders", "unique_fills", "unique_opened_positions"],
        },
        "source": str(state["path"]),
        "fully_reconciled": not problems,
        "integrity_status": integrity,
        "counts": counts,
        "problems": problems,
        "authoritative_sources": state["sources"],
        "canonical_lineage": {
            "orders": sorted(state["unique_submitted"].keys()),
            "fills": [event.key for event in fill_events],
            "positions": state.get("position_lineage") or state["position_state"]["positions"],
        },
        "order_reconciliation": state.get("order_reconciliation", {}),
        "broker_acceptance_explanation": broker_acceptance_explanation,
        "shadow_order_rows": [src.row for src in state.get("shadow_order_sources", [])],
        "duplicate_shadow_rows": [src.row for src in state.get("duplicate_shadow_sources", [])],
        "historical_reconstruction": {
            "counted_as_reconciled": not problems,
            "classifications": {
                "EXACT_MATCH": len([e for e in fill_events if e.classification == "EXACT_MATCH"]),
                "DETERMINISTIC_RECOVERY": len([e for e in fill_events if e.classification == "DETERMINISTIC_RECOVERY"]),
                "DUPLICATE": len(state["duplicate_fill_sources"]) + len(state["duplicate_order_sources"]),
                "AMBIGUOUS": len([src for src in state["order_sources"] if _is_synthetic_or_replay(src.row)]),
                "SHADOW_EXPECTED": len([src for src in state["order_sources"] if _is_shadow_row(src.row)]),
                "UNMATCHED": len([p for p in problems if str(p.get("kind", "")).startswith("unmatched")]),
            },
        },
        "integrity_incidents": _load_integrity_incidents(root, user, day=day),
    }
    return ReconciliationResult(report, problems)


def _broker_order_row(
    *,
    order: Any,
    submitted_row: Mapping[str, Any] | None,
    position: Any | None,
    order_id: str,
    symbol: str,
    user: str,
    day: str,
) -> dict[str, Any]:
    status = _normalize_broker_enum(_obj_value(order, "status")) or "unknown"
    side = _normalize_broker_enum(_obj_value(order, "side") or (submitted_row or {}).get("action") or "buy")
    qty = _safe_float(_obj_value(order, "qty", "quantity"))
    filled_qty = _safe_float(_obj_value(order, "filled_qty"))
    filled_avg = _safe_float(_obj_value(order, "filled_avg_price", "filled_average_price"))
    recovery_method = "exact_order_id_order_status"
    confidence = "exact"
    if (filled_qty or 0.0) <= 0.0 and position is not None:
        pos_qty = _position_qty(position)
        if pos_qty > 0.0:
            filled_qty = pos_qty
            notional = _position_notional(position)
            if filled_avg is None and notional > 0.0:
                filled_avg = notional / pos_qty
            status = "filled"
            recovery_method = "unique_order_current_position"
            confidence = "deterministic"
    submitted_at = _obj_value(order, "submitted_at", "created_at")
    created_at = _obj_value(order, "created_at", "submitted_at")
    filled_at = _obj_value(order, "filled_at")
    cancelled_at = _obj_value(order, "canceled_at", "cancelled_at")
    rejected_at = _obj_value(order, "rejected_at")
    expired_at = _obj_value(order, "expired_at")
    replaced_at = _obj_value(order, "replaced_at")
    updated_at = _obj_value(order, "updated_at")
    event_ts = filled_at or cancelled_at or rejected_at or expired_at or replaced_at or updated_at or submitted_at or created_at
    notional = None
    if filled_qty and filled_avg:
        notional = filled_qty * filled_avg
    elif qty and (submitted_row or {}).get("final_reference_price"):
        notional = qty * float((submitted_row or {}).get("final_reference_price"))
    return {
        "symbol": symbol,
        "action": side,
        "route": (submitted_row or {}).get("route") or "trend_long",
        "source": (submitted_row or {}).get("source") or (submitted_row or {}).get("route") or "trend_long",
        "strategy": (submitted_row or {}).get("strategy") or (submitted_row or {}).get("route") or "trend_long",
        "user_id": user,
        "broker_order_id": order_id,
        "order_id": order_id,
        "client_order_id": _obj_value(order, "client_order_id"),
        "submitted_at": str(submitted_at) if submitted_at is not None else None,
        "submitted": True,
        "submit_attempt": True,
        "status": status,
        "broker_status": status,
        "broker_status_timestamp": str(event_ts) if event_ts is not None else None,
        "reconciled_at": datetime.now(timezone.utc).isoformat(),
        "terminal_state": canonical_order_state({"status": status}),
        "decision_id": (submitted_row or {}).get("decision_id"),
        "logical_order_id": (submitted_row or {}).get("logical_order_id") or (submitted_row or {}).get("order_id") or order_id,
        "qty": qty,
        "notional": notional,
        "filled_qty": filled_qty,
        "filled_avg_price": filled_avg,
        "created_at": str(created_at) if created_at is not None else None,
        "filled_at": str(filled_at) if filled_at is not None else None,
        "cancelled_at": str(cancelled_at) if cancelled_at is not None else None,
        "rejected_at": str(rejected_at) if rejected_at is not None else None,
        "expired_at": str(expired_at) if expired_at is not None else None,
        "replaced_at": str(replaced_at) if replaced_at is not None else None,
        "replaces": _obj_value(order, "replaces"),
        "replaced_by": _obj_value(order, "replaced_by"),
        "event_timestamp_utc": str(event_ts) if event_ts is not None else datetime.now(timezone.utc).isoformat(),
        "broker_dispatch_attempted": True,
        "execution_allowed": True,
        "event_origin": "broker_reconciliation",
        "recovered": True,
        "recovery_method": recovery_method,
        "confidence": confidence,
        "allocator_requested_notional": (submitted_row or {}).get("allocator_requested_notional"),
        "allocator_requested_qty": (submitted_row or {}).get("allocator_requested_qty"),
        "bounded_pilot_applied": (submitted_row or {}).get("bounded_pilot_applied"),
        "final_submitted_qty": (submitted_row or {}).get("final_submitted_qty") or qty,
        "final_reference_price": (submitted_row or {}).get("final_reference_price"),
        "final_estimated_notional": (submitted_row or {}).get("final_estimated_notional") or notional,
    }


def _broker_fill_activity_rows(
    *,
    broker: Any,
    order_id: str,
    submitted_row: Mapping[str, Any] | None,
    symbol: str,
    user: str,
) -> list[dict[str, Any]]:
    """Return broker-native fill/activity rows for one order when available."""

    activity_getters = (
        getattr(broker, "get_order_activities", None),
        getattr(broker, "get_fill_activities", None),
        getattr(broker, "get_account_activities", None),
    )
    raw_activities: list[Any] = []
    for getter in activity_getters:
        if not callable(getter):
            continue
        try:
            raw = getter(order_id=order_id)
        except TypeError:
            try:
                raw = getter()
            except TypeError:
                continue
        raw_activities = list(raw or [])
        if raw_activities:
            break
    rows: list[dict[str, Any]] = []
    for activity in raw_activities:
        activity_order_id = _norm_id(_obj_value(activity, "order_id", "orderID"))
        if activity_order_id and activity_order_id != order_id:
            continue
        activity_type = _normalize_broker_enum(_obj_value(activity, "activity_type", "type"))
        if activity_type and activity_type not in {"fill", "partial_fill"}:
            continue
        qty = _safe_float(_obj_value(activity, "qty", "quantity"))
        price = _safe_float(_obj_value(activity, "price", "fill_price"))
        if qty is None or qty <= 0:
            continue
        ts = _obj_value(activity, "transaction_time", "timestamp", "filled_at", "created_at")
        activity_id = _norm_id(_obj_value(activity, "id", "activity_id", "broker_activity_id"))
        rows.append(
            {
                "symbol": symbol,
                "action": _normalize_broker_enum(_obj_value(activity, "side") or (submitted_row or {}).get("action") or "buy"),
                "route": (submitted_row or {}).get("route") or "trend_long",
                "source": (submitted_row or {}).get("source") or (submitted_row or {}).get("route") or "trend_long",
                "strategy": (submitted_row or {}).get("strategy") or (submitted_row or {}).get("route") or "trend_long",
                "user_id": user,
                "broker_order_id": order_id,
                "order_id": order_id,
                "broker_activity_id": activity_id or None,
                "broker_fill_id": activity_id or None,
                "submitted": True,
                "submit_attempt": True,
                "status": "filled",
                "broker_status": "filled",
                "broker_status_timestamp": str(ts) if ts is not None else None,
                "reconciled_at": datetime.now(timezone.utc).isoformat(),
                "terminal_state": "FILLED",
                "decision_id": (submitted_row or {}).get("decision_id"),
                "logical_order_id": (submitted_row or {}).get("logical_order_id") or (submitted_row or {}).get("order_id") or order_id,
                "qty": qty,
                "filled_qty": qty,
                "filled_avg_price": price,
                "fill_price": price,
                "filled_at": str(ts) if ts is not None else None,
                "event_timestamp_utc": str(ts) if ts is not None else datetime.now(timezone.utc).isoformat(),
                "broker_dispatch_attempted": True,
                "execution_allowed": True,
                "event_origin": "broker_reconciliation",
                "recovered": True,
                "recovery_method": "broker_activity_fill",
                "confidence": "exact" if activity_id else "deterministic",
                "allocator_requested_notional": (submitted_row or {}).get("allocator_requested_notional"),
                "allocator_requested_qty": (submitted_row or {}).get("allocator_requested_qty"),
                "bounded_pilot_applied": (submitted_row or {}).get("bounded_pilot_applied"),
                "final_submitted_qty": qty,
                "final_reference_price": (submitted_row or {}).get("final_reference_price"),
                "final_estimated_notional": qty * price if price is not None else None,
            }
        )
    return rows


def reconcile_broker_order_lifecycle(
    *,
    root: Path = PROJECT_ROOT,
    day: str,
    user: str,
    broker: Any,
    order_id: str,
    symbol: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Read-only broker reconciliation with optional idempotent local recovery persistence."""

    state = build_lifecycle_state(root=root, day=day, user=user)
    target_order_id = _norm_id(order_id)
    if not target_order_id:
        raise ValueError("order_id is required")
    target_symbol = _norm_symbol(symbol)
    matching_submitted = [
        src.row
        for src in state["submitted_sources"]
        if src.row.get("event_origin") != "broker_reconciliation"
        and src.row.get("recovered") is not True
        and (
            target_order_id in {
            _norm_id(src.row.get("order_id")),
            _norm_id(src.row.get("broker_order_id")),
            _norm_id(src.row.get("client_order_id")),
            canonical_order_key(src.row),
            }
            and (not target_symbol or _norm_symbol(src.row.get("symbol")) == target_symbol)
        )
    ]
    submitted_row = matching_submitted[0] if len(matching_submitted) == 1 else None
    get_order = getattr(broker, "get_order", None)
    raw_order = get_order(target_order_id) if callable(get_order) else getattr(broker, "_trading").get_order_by_id(target_order_id)
    broker_symbol = _norm_symbol(_obj_value(raw_order, "symbol") or target_symbol or (submitted_row or {}).get("symbol"))
    if target_symbol and broker_symbol != target_symbol:
        return {
            "reconciled": False,
            "reason": "broker_order_symbol_mismatch",
            "order_id": target_order_id,
            "broker_symbol": broker_symbol,
            "requested_symbol": target_symbol,
        }
    positions = []
    list_positions = getattr(broker, "get_positions", None)
    if callable(list_positions):
        positions = list(list_positions() or [])
    matching_positions = [p for p in positions if _norm_symbol(_obj_value(p, "symbol", "asset_symbol")) == broker_symbol and _position_qty(p) > 0.0]
    position = matching_positions[0] if len(matching_positions) == 1 else None
    if len(matching_positions) > 1:
        return {"reconciled": False, "reason": "multiple_matching_broker_positions", "order_id": target_order_id, "symbol": broker_symbol}
    if not submitted_row:
        same_symbol_submissions = [
            src.row
            for src in state["submitted_sources"]
            if src.row.get("event_origin") != "broker_reconciliation"
            and src.row.get("recovered") is not True
            and _norm_symbol(src.row.get("symbol")) == broker_symbol
        ]
        if len(same_symbol_submissions) == 1:
            submitted_row = same_symbol_submissions[0]
        elif position is not None and _normalize_broker_enum(_obj_value(raw_order, "side")) != "sell":
            return {"reconciled": False, "reason": "multiple_or_missing_candidate_orders", "order_id": target_order_id, "symbol": broker_symbol}
    recovered_row = _broker_order_row(
        order=raw_order,
        submitted_row=submitted_row,
        position=position,
        order_id=target_order_id,
        symbol=broker_symbol,
        user=user,
        day=day,
    )
    status = str(recovered_row.get("status") or "").lower()
    filled_qty = _safe_float(recovered_row.get("filled_qty")) or 0.0
    terminal_state = canonical_order_state(recovered_row)
    if terminal_state == "UNKNOWN_BROKER_STATE":
        recovered_row["terminal_state"] = terminal_state
        recovered_row["recovery_method"] = "exact_order_id_unknown_broker_status"
    if status in {"filled", "partially_filled"} and filled_qty <= 0.0:
        return {"reconciled": False, "reason": "filled_status_without_quantity", "order_id": target_order_id, "status": status}
    path = None
    activity_rows: list[dict[str, Any]] = []
    if persist:
        activity_rows = _broker_fill_activity_rows(
            broker=broker,
            order_id=target_order_id,
            submitted_row=submitted_row,
            symbol=broker_symbol,
            user=user,
        )
        if activity_rows:
            recovered_row["fill_summary_only"] = True
        rows_to_persist = [recovered_row, *activity_rows]
        for row in rows_to_persist:
            path = record_recovered_order_event(
                data_dir=root / "data",
                user_id=user,
                day=day,
                timestamp=row.get("event_timestamp_utc") or datetime.now(timezone.utc),
                record=row,
            )
    refreshed = build_lifecycle_state(root=root, day=day, user=user)
    counts = refreshed["counts"]
    return {
        "reconciled": True,
        "date": day,
        "user": user,
        "symbol": broker_symbol,
        "broker_order_id": target_order_id,
        "broker_order_status": status,
        "terminal_state": terminal_state,
        "broker_status_timestamp": recovered_row.get("broker_status_timestamp"),
        "created_timestamp": recovered_row.get("created_at"),
        "submitted_timestamp": recovered_row.get("submitted_at"),
        "filled_timestamp": recovered_row.get("filled_at"),
        "cancelled_timestamp": recovered_row.get("cancelled_at"),
        "rejected_timestamp": recovered_row.get("rejected_at"),
        "expired_timestamp": recovered_row.get("expired_at"),
        "replacement_relation": {
            "replaces": recovered_row.get("replaces"),
            "replaced_by": recovered_row.get("replaced_by"),
        },
        "broker_created_order": True,
        "local_persistence_missed_state": bool(submitted_row) and submitted_row.get("status") != status,
        "submitted_quantity": _safe_float(recovered_row.get("qty")),
        "filled_quantity": filled_qty,
        "filled_avg_price": _safe_float(recovered_row.get("filled_avg_price")),
        "actual_notional": _safe_float(recovered_row.get("notional")),
        "recovery_method": recovered_row.get("recovery_method"),
        "confidence": recovered_row.get("confidence"),
        "persisted_path": str(path) if path else None,
        "canonical_counts": {
            "submitted_orders": counts.get("submitted_orders"),
            "broker_accepted_orders": counts.get("broker_accepted_orders"),
            "broker_confirmed_orders": counts.get("broker_confirmed_orders"),
            "broker_current_orders": counts.get("broker_current_orders"),
            "broker_terminal_orders": counts.get("broker_terminal_orders"),
            "broker_filled_orders": counts.get("broker_filled_orders"),
            "broker_rejected_orders": counts.get("broker_rejected_orders"),
            "broker_cancelled_orders": counts.get("broker_cancelled_orders"),
            "broker_unresolved_orders": counts.get("broker_unresolved_orders"),
            "completed_fills": counts.get("completed_fills"),
            "opened_positions": counts.get("opened_positions"),
            "still_open_positions": counts.get("still_open_positions"),
            "duplicate_order_events": counts.get("duplicate_order_events"),
            "duplicate_fill_events": counts.get("duplicate_fill_events"),
        },
    }


def reconcile_submitted_broker_orders(
    *,
    root: Path = PROJECT_ROOT,
    day: str,
    user: str,
    broker: Any,
    persist: bool = True,
) -> dict[str, Any]:
    """Reconcile every current-day local live submitted order by broker order id."""

    state = build_lifecycle_state(root=root, day=day, user=user)
    submitted = []
    for key, group in sorted((state.get("unique_submitted") or {}).items()):
        latest = sorted(
            group,
            key=lambda src: ((event_timestamp(src.row) or datetime.min.replace(tzinfo=timezone.utc)), src.index),
        )[-1]
        row = latest.row
        broker_order_id = _norm_id(row.get("broker_order_id") or row.get("order_id"))
        if not broker_order_id:
            submitted.append(
                {
                    "canonical_order_key": key,
                    "reconciled": False,
                    "reason": "missing_broker_order_id",
                    "symbol": row.get("symbol"),
                }
            )
            continue
        submitted.append(
            reconcile_broker_order_lifecycle(
                root=root,
                day=day,
                user=user,
                broker=broker,
                order_id=broker_order_id,
                symbol=row.get("symbol"),
                persist=persist,
            )
        )
    refreshed = build_lifecycle_state(root=root, day=day, user=user)
    return {
        "date": day,
        "user": user,
        "persisted": persist,
        "submitted_order_count": len(submitted),
        "reconciled_count": len([row for row in submitted if row.get("reconciled")]),
        "unresolved_count": len([row for row in submitted if not row.get("reconciled")]),
        "orders": submitted,
        "canonical_counts": refreshed.get("counts") or {},
        "order_reconciliation": refreshed.get("order_reconciliation") or {},
    }


def _load_integrity_incidents(root: Path, user: str, *, day: str | None = None, include_expected_blocks: bool = False) -> list[dict[str, Any]]:
    path = root / "data" / "integrity" / f"{user}.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [{"reason_code": "ENTRY_BLOCKED_INTEGRITY_FAILURE", "detail": "integrity state unreadable"}]
    incidents = payload.get("incidents") if isinstance(payload, Mapping) else []
    if not isinstance(incidents, list):
        return []
    out: list[dict[str, Any]] = []
    for row in incidents:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        if day is not None and event_trading_date_et(item) != day:
            continue
        if not include_expected_blocks and is_expected_entry_block(item.get("reason_code")):
            continue
        out.append(item)
    return out


def build_underlying_signal_record(row: Mapping[str, Any]) -> dict[str, Any]:
    symbol = _norm_symbol(row.get("underlying_symbol", row.get("symbol")))
    return {
        "symbol": symbol,
        "strategy_route": row.get("route") or row.get("entry_route") or row.get("source"),
        "signal_timestamp": row.get("signal_timestamp") or row.get("timestamp"),
        "signal_price": _safe_float(row.get("signal_price") or row.get("price")),
        "approval_timestamp": row.get("approval_timestamp"),
        "approval_price": _safe_float(row.get("approval_price")),
        "order_timestamp": row.get("order_timestamp") or row.get("submitted_at"),
        "underlying_price_at_order": _safe_float(row.get("underlying_price_at_order")),
        "fill_timestamp": row.get("fill_timestamp") or row.get("filled_at"),
        "underlying_price_at_fill": _safe_float(row.get("underlying_price_at_fill")),
        "spy_price_at_signal": _safe_float(row.get("spy_price_at_signal")),
        "sector_etf_price_at_signal": _safe_float(row.get("sector_etf_price_at_signal")),
        "forward_returns": {
            "1m": _safe_float(row.get("forward_return_1m")),
            "5m": _safe_float(row.get("forward_return_5m")),
            "15m": _safe_float(row.get("forward_return_15m")),
            "30m": _safe_float(row.get("forward_return_30m")),
            "60m": _safe_float(row.get("forward_return_60m")),
            "session_close": _safe_float(row.get("session_close_return")),
        },
        "spy_relative_return": _safe_float(row.get("spy_relative_return")),
        "sector_relative_return": _safe_float(row.get("sector_relative_return")),
        "underlying_mfe": _safe_float(row.get("underlying_mfe", row.get("mfe_pct"))),
        "underlying_mae": _safe_float(row.get("underlying_mae", row.get("mae_pct"))),
        "time_to_mfe": row.get("time_to_mfe"),
        "time_to_mae": row.get("time_to_mae"),
        "data_status": "available" if any(k in row for k in ("forward_return_1m", "forward_return_5m", "mfe_pct")) else "unavailable",
    }


def build_option_execution_record(row: Mapping[str, Any]) -> dict[str, Any]:
    symbol = _norm_symbol(row.get("option_symbol", row.get("symbol")))
    parsed = parse_occ_equity_option_symbol(symbol)
    underlying, exp, right, strike = parsed if parsed else (row.get("underlying_symbol"), None, None, None)
    bid = _safe_float(row.get("bid_at_fill", row.get("bid")))
    ask = _safe_float(row.get("ask_at_fill", row.get("ask")))
    fill = _safe_float(row.get("filled_avg_price", row.get("actual_fill_price")))
    return {
        "option_symbol": symbol,
        "underlying_symbol": underlying,
        "strike": _safe_float(row.get("strike", strike)),
        "expiration": exp.isoformat() if exp else row.get("expiration"),
        "right": row.get("right") or right,
        "days_to_expiration": _safe_int(row.get("days_to_expiration", row.get("dte"))),
        "delta": _safe_float(row.get("delta")),
        "gamma": _safe_float(row.get("gamma")),
        "theta": _safe_float(row.get("theta")),
        "implied_volatility": _safe_float(row.get("implied_volatility", row.get("iv"))),
        "bid_at_signal": _safe_float(row.get("bid_at_signal")),
        "ask_at_signal": _safe_float(row.get("ask_at_signal")),
        "mid_at_signal": _safe_float(row.get("mid_at_signal")),
        "bid_at_submission": _safe_float(row.get("bid_at_submission")),
        "ask_at_submission": _safe_float(row.get("ask_at_submission")),
        "bid_at_fill": bid,
        "ask_at_fill": ask,
        "actual_fill_price": fill,
        "spread_dollars": (ask - bid) if bid is not None and ask is not None else None,
        "spread_percentage": calculate_spread_pct(bid=bid, ask=ask),
        "slippage_from_mid": calculate_slippage_from_mid(fill_price=fill, bid=bid, ask=ask),
        "open_interest": _safe_int(row.get("open_interest")),
        "volume": _safe_int(row.get("volume")),
        "option_mfe": _safe_float(row.get("option_mfe", row.get("mfe_pct"))),
        "option_mae": _safe_float(row.get("option_mae", row.get("mae_pct"))),
        "time_to_mfe": row.get("time_to_mfe"),
        "time_to_mae": row.get("time_to_mae"),
        "exit_fill": _safe_float(row.get("exit_fill")),
        "realized_pnl": _safe_float(row.get("realized_pnl", row.get("pnl"))),
        "estimated_spread_cost": _safe_float(row.get("estimated_spread_cost")),
        "estimated_slippage_cost": _safe_float(row.get("estimated_slippage_cost")),
        "data_status": "available" if fill is not None or bid is not None or ask is not None else "unavailable",
    }


def latency_report(row: Mapping[str, Any]) -> dict[str, Any]:
    stages = [
        "setup_valid",
        "candidate_selected",
        "entry_eval_started",
        "entry_approved",
        "alert_generated",
        "order_submitted",
        "broker_acknowledged",
        "fill_received",
        "position_persisted",
    ]
    times = {stage: _parse_ts(row.get(f"{stage}_timestamp")) for stage in stages}
    latencies: dict[str, float | None] = {}
    for left, right in zip(stages, stages[1:]):
        l, r = times[left], times[right]
        latencies[f"{left}_to_{right}_seconds"] = (r - l).total_seconds() if l and r else None
    return {"timestamps": {k: v.isoformat() if v else None for k, v in times.items()}, "latencies": latencies}


def latency_blocks(row: Mapping[str, Any], thresholds: Mapping[str, Any]) -> list[dict[str, Any]]:
    checks = {
        "ENTRY_BLOCKED_STALE_UNDERLYING_QUOTE": ("underlying_quote_age_seconds", "max_underlying_quote_age_seconds"),
        "ENTRY_BLOCKED_STALE_OPTION_QUOTE": ("option_quote_age_seconds", "max_option_quote_age_seconds"),
        "ENTRY_BLOCKED_STALE_SIGNAL": ("signal_age_seconds", "max_signal_age_seconds"),
        "ENTRY_BLOCKED_EXECUTION_LATENCY": ("signal_to_order_seconds", "max_signal_to_order_seconds"),
        "ENTRY_BLOCKED_NEWS_TOO_OLD": ("news_age_seconds", "max_news_age_seconds"),
    }
    out: list[dict[str, Any]] = []
    for code, (observed_key, threshold_key) in checks.items():
        observed = _safe_float(row.get(observed_key))
        threshold = _safe_float(thresholds.get(threshold_key))
        if observed is not None and threshold is not None and observed > threshold:
            out.append({"reason_code": code, "observed": observed, "threshold": threshold})
    return out


def contract_quality_audit(candidate: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    bid = _safe_float(candidate.get("bid"))
    ask = _safe_float(candidate.get("ask"))
    spread = _safe_float(candidate.get("spread_pct"))
    if spread is None:
        spread = calculate_spread_pct(bid=bid, ask=ask)
    checks = {
        "maximum_spread_percentage": (spread, thresholds.get("maximum_spread_percentage"), lambda obs, thr: obs <= thr),
        "minimum_volume": (_safe_float(candidate.get("volume")), thresholds.get("minimum_volume"), lambda obs, thr: obs >= thr),
        "minimum_open_interest": (_safe_float(candidate.get("open_interest")), thresholds.get("minimum_open_interest"), lambda obs, thr: obs >= thr),
        "minimum_delta": (abs(_safe_float(candidate.get("delta")) or 0.0), thresholds.get("minimum_delta"), lambda obs, thr: obs >= thr),
        "maximum_strike_distance": (_safe_float(candidate.get("strike_distance_pct")), thresholds.get("maximum_strike_distance"), lambda obs, thr: obs <= thr),
        "minimum_dte": (_safe_float(candidate.get("dte")), thresholds.get("minimum_dte"), lambda obs, thr: obs >= thr),
        "maximum_dte": (_safe_float(candidate.get("dte")), thresholds.get("maximum_dte"), lambda obs, thr: obs <= thr),
        "maximum_quote_age": (_safe_float(candidate.get("quote_age_seconds")), thresholds.get("maximum_quote_age"), lambda obs, thr: obs <= thr),
        "maximum_expected_slippage": (_safe_float(candidate.get("expected_slippage")), thresholds.get("maximum_expected_slippage"), lambda obs, thr: obs <= thr),
    }
    metrics: dict[str, Any] = {"spread_pct": spread}
    for name, (observed, raw_threshold, predicate) in checks.items():
        threshold = _safe_float(raw_threshold)
        metrics[name] = {"observed": observed, "threshold": threshold}
        if threshold is not None and (observed is None or not predicate(float(observed), float(threshold))):
            reasons.append(name)
    return {
        "contract": dict(candidate),
        "accepted": not reasons,
        "rejection_reasons": reasons,
        "metrics": metrics,
        "selection_score": 0.0 if reasons else 1.0,
    }


def profitability_report(*, root: Path = PROJECT_ROOT, start: str, end: str, user: str, mode: str | None = None, strategy: str | None = None) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    unreconciled_days: list[str] = []
    for day in _date_range(start, end):
        audit = run_trading_audit(root=root, day=day, user=user)
        if not audit.ok:
            unreconciled_days.append(day)
        payload = _load_daily(root, day, user)
        for row in _rows(payload, "exits"):
            r = dict(row)
            r.setdefault("date", day)
            r.setdefault("route", r.get("entry_route") or r.get("entry_source") or "unknown")
            if strategy and str(r.get("route")) != strategy and str(r.get("entry_route")) != strategy:
                continue
            if mode and str(r.get("mode") or "live").lower() != mode:
                continue
            trades.append(r)
    pnl = [_safe_float(t.get("realized_pnl", t.get("pnl"))) for t in trades]
    pnl = [v for v in pnl if v is not None]
    wins = [v for v in pnl if v > 0]
    losses = [v for v in pnl if v < 0]
    win_rate = len(wins) / len(pnl) if pnl else None
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    net_expectancy = (win_rate * avg_win - (1 - win_rate) * avg_loss) if win_rate is not None else None
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    by = _breakdowns(trades)
    report = {
        "from": start,
        "to": end,
        "user": user,
        "scope": {
            "name": "profitability_reconciled_exits",
            "source": "trade_attribution.exits plus canonical trading-audit status",
            "date_basis": "event_timestamp_america_new_york",
            "environment_filter": mode or "all",
            "record_origin_filter": "only reconciled days count as reconciled trades",
        },
        "mode": mode,
        "strategy": strategy,
        "reconciled": not unreconciled_days,
        "unreconciled_days": unreconciled_days,
        "sample_size_warning": len(pnl) < 30,
        "metrics": {
            "number_of_reconciled_trades": 0 if unreconciled_days else len(pnl),
            "win_rate": win_rate,
            "average_win": avg_win,
            "average_loss": avg_loss,
            "gross_expectancy": (sum(pnl) / len(pnl)) if pnl else None,
            "net_expectancy": net_expectancy,
            "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
            "realized_pnl": sum(pnl),
            "estimated_spread_cost": sum(_safe_float(t.get("estimated_spread_cost")) or 0.0 for t in trades),
            "estimated_slippage_cost": sum(_safe_float(t.get("estimated_slippage_cost")) or 0.0 for t in trades),
            "maximum_drawdown": _max_drawdown(pnl),
            "median_holding_time": _median([_safe_float(t.get("holding_minutes", t.get("hold_minutes"))) for t in trades]),
            "average_mfe": _avg([_safe_float(t.get("mfe_pct", t.get("max_favorable_excursion_pct"))) for t in trades]),
            "average_mae": _avg([_safe_float(t.get("mae_pct", t.get("max_adverse_excursion_pct"))) for t in trades]),
            "mfe_capture_ratio": _avg([mfe_capture_ratio(realized_profit=_safe_float(t.get("realized_pnl", t.get("pnl"))), mfe=_safe_float(t.get("mfe_pct", t.get("max_favorable_excursion_pct")))) for t in trades]),
            "signal_to_order_latency": _avg([_safe_float(t.get("signal_to_order_seconds")) for t in trades]),
            "signal_to_fill_latency": _avg([_safe_float(t.get("signal_to_fill_seconds")) for t in trades]),
            "alert_to_fill_latency": _avg([_safe_float(t.get("alert_to_fill_seconds")) for t in trades]),
            "entered_after_50pct_mfe_pct": _pct([(_safe_float(t.get("entered_after_mfe_pct")) or 0.0) >= 50.0 for t in trades]),
            "entered_after_75pct_mfe_pct": _pct([(_safe_float(t.get("entered_after_mfe_pct")) or 0.0) >= 75.0 for t in trades]),
        },
        "breakdowns": by,
        "loss_attribution": Counter(classify_loss(t, reconciled=not unreconciled_days)["primary"] for t in trades if (_safe_float(t.get("realized_pnl", t.get("pnl"))) or 0.0) < 0),
    }
    report["loss_attribution"] = dict(report["loss_attribution"])
    return report


def _avg(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _median(values: Iterable[float | None]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _pct(values: Sequence[bool]) -> float | None:
    return (sum(1 for v in values if v) / len(values) * 100.0) if values else None


def _max_drawdown(pnl: Sequence[float]) -> float:
    peak = 0.0
    equity = 0.0
    dd = 0.0
    for v in pnl:
        equity += v
        peak = max(peak, equity)
        dd = min(dd, equity - peak)
    return dd


def _breakdowns(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    keys = {
        "strategy_route": ("route", "entry_route"),
        "market_regime": ("market_regime_label",),
        "entry_hour": ("entry_hour",),
        "day_of_week": ("day_of_week",),
        "symbol": ("symbol",),
        "sector": ("sector", "sector_etf"),
        "news_provider": ("news_provider",),
        "exit_reason": ("exit_reason",),
        "loss_attribution": ("loss_attribution",),
        "mode": ("mode",),
    }
    out: dict[str, Any] = {}
    for name, candidates in keys.items():
        agg: dict[str, list[float]] = defaultdict(list)
        for trade in trades:
            label = next((str(trade.get(k)) for k in candidates if trade.get(k) not in (None, "")), "unknown")
            pnl = _safe_float(trade.get("realized_pnl", trade.get("pnl")))
            if pnl is not None:
                agg[label].append(pnl)
        out[name] = {k: {"count": len(v), "pnl": sum(v), "expectancy": sum(v) / len(v), "sample_size_warning": len(v) < 30} for k, v in sorted(agg.items())}
    # Numeric band fields.
    for field in ("quality_score", "spread_pct", "delta", "dte", "option_volume", "option_open_interest", "news_age"):
        out[f"{field}_band"] = _band_breakdown(trades, field)
    return out


def _band_breakdown(trades: Sequence[Mapping[str, Any]], field: str) -> dict[str, Any]:
    agg: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        v = _safe_float(trade.get(field))
        label = "unavailable" if v is None else ("low" if v < 1 else "mid" if v < 5 else "high")
        pnl = _safe_float(trade.get("realized_pnl", trade.get("pnl")))
        if pnl is not None:
            agg[label].append(pnl)
    return {k: {"count": len(v), "pnl": sum(v), "expectancy": sum(v) / len(v), "sample_size_warning": len(v) < 30} for k, v in sorted(agg.items())}


def news_edge_report(*, root: Path = PROJECT_ROOT, start: str, end: str) -> dict[str, Any]:
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    if end_date < start_date:
        raise ValueError("--to must be on or after --from")
    events_by_key: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    for path in sorted((root / "data" / "premarket").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_events = payload.get("events") or payload.get("articles") or payload.get("catalysts") or []
        if isinstance(raw_events, list):
            for row in raw_events:
                if not isinstance(row, Mapping):
                    continue
                event = _normalize_news_event(row)
                if not _news_event_in_range(event, start_date=start_date, end_date=end_date):
                    continue
                key = (
                    str(event.get("provider") or "unknown"),
                    str(event.get("article_id") or event.get("headline_hash") or ""),
                    str(event.get("published_time") or event.get("ingested_time") or ""),
                )
                events_by_key.setdefault(key, event)
    events = sorted(
        events_by_key.values(),
        key=lambda row: str(row.get("published_time") or row.get("ingested_time") or ""),
    )
    provider_counts = Counter(e.get("provider") or "unknown" for e in events)
    recommendation = "INSUFFICIENT_DATA"
    return {
        "from": start,
        "to": end,
        "scope": {
            "name": "news_edge",
            "source": "data/premarket persisted news events",
            "date_basis": "published_or_ingested_timestamp",
            "environment_filter": "not_applicable",
            "record_origin_filter": "news events only; no broker lifecycle counts",
            "integrity_status": {"status": "INSUFFICIENT_DATA", "reasons": ["article-to-trade links unavailable"]},
        },
        "events": events,
        "provider_counts": dict(provider_counts),
        "comparisons": {
            "news_candidates_traded": 0,
            "news_candidates_rejected": 0,
            "non_news_similar_technical_setup": 0,
            "incremental_expectancy": None,
            "status": "unavailable_without_reconciled_trade_links",
        },
        "recommendation": recommendation,
    }


def _news_event_in_range(event: Mapping[str, Any], *, start_date: date, end_date: date) -> bool:
    ts = _parse_ts(event.get("published_time")) or _parse_ts(event.get("ingested_time"))
    if ts is None:
        return False
    event_date = ts.astimezone(ET).date()
    return start_date <= event_date <= end_date


def _normalize_news_event(row: Mapping[str, Any]) -> dict[str, Any]:
    headline = str(row.get("headline") or row.get("title") or "")
    return {
        "provider": row.get("provider") or row.get("source") or "unknown",
        "article_id": row.get("id") or row.get("article_id") or hashlib.sha256(headline.encode("utf-8")).hexdigest()[:16],
        "headline_hash": hashlib.sha256(headline.encode("utf-8")).hexdigest() if headline else None,
        "published_time": row.get("published_at") or row.get("published_time"),
        "ingested_time": row.get("ingested_at") or row.get("timestamp"),
        "first_evaluated_time": row.get("first_evaluated_time"),
        "first_alert_time": row.get("first_alert_time"),
        "first_order_time": row.get("first_order_time"),
        "first_fill_time": row.get("first_fill_time"),
        "symbols_extracted": row.get("symbols") or row.get("symbols_extracted") or [],
        "symbol_relevance_confidence": _safe_float(row.get("symbol_relevance_confidence")),
        "sentiment": row.get("sentiment"),
        "event_category": row.get("event_category") or row.get("category"),
        "filing_type": row.get("filing_type"),
        "prices": {
            "publication": _safe_float(row.get("price_at_publication")),
            "ingestion": _safe_float(row.get("price_at_ingestion")),
            "signal": _safe_float(row.get("price_at_signal")),
            "fill": _safe_float(row.get("price_at_fill")),
        },
        "forward_returns": row.get("forward_returns", {}),
        "spy_relative_returns": row.get("spy_relative_returns", {}),
        "trade_outcome": row.get("trade_outcome"),
    }


def strategy_readiness_report(*, root: Path = PROJECT_ROOT, config: Mapping[str, Any] | None = None, user: str = "live_bot") -> dict[str, Any]:
    states = strategy_states(config or {})
    rows: list[dict[str, Any]] = []
    for route, state in states.items():
        perf = profitability_report(root=root, start="2026-07-01", end="2026-07-20", user=user, strategy=route)
        metrics = perf["metrics"]
        raw_net = metrics.get("net_expectancy")
        raw_pf = metrics.get("profit_factor")
        sample = metrics.get("number_of_reconciled_trades") or 0
        integrity_ok = bool(perf.get("reconciled"))
        reconciled_net = raw_net if integrity_ok and sample > 0 else None
        reconciled_pf = raw_pf if integrity_ok and sample > 0 else None
        recommendation = "INSUFFICIENT_DATA"
        if not integrity_ok:
            recommendation = "FIX_DATA"
        elif sample == 0:
            recommendation = "INSUFFICIENT_DATA"
        elif sample >= 30 and reconciled_net is not None and reconciled_net > 0 and (reconciled_pf is not None and reconciled_pf >= 1.2):
            recommendation = "KEEP" if state.state == "LIVE" else "eligible_for_manual_review"
        rows.append(
            {
                "route": route,
                "state": state.state,
                "sample_size": sample,
                "reconciled_sample_size": sample,
                "hypothetical_sample_size": 0,
                "raw_historical_expectancy": raw_net,
                "reconciled_expectancy": reconciled_net,
                "hypothetical_shadow_expectancy": None,
                "net_expectancy": reconciled_net,
                "profit_factor": reconciled_pf,
                "data_integrity_status": "ok" if integrity_ok else "unreconciled",
                "last_evaluated_date": "2026-07-20",
                "recommendation": recommendation,
            }
        )
    return {
        "user": user,
        "scope": {
            "name": "strategy_readiness",
            "source": "canonical profitability report",
            "date_basis": "event_timestamp_america_new_york",
            "environment_filter": "live_bot",
            "record_origin_filter": "reconciled trades only for production readiness",
        },
        "strategies": rows,
    }


def validate_experiment(exp: Mapping[str, Any]) -> list[str]:
    required = {"variable", "baseline_value", "candidate_value", "eligible_strategies", "start_date", "mode", "minimum_sample_size", "success_criterion", "maximum_loss_or_degradation_limit"}
    errors = [f"missing:{key}" for key in sorted(required - set(exp))]
    variable = exp.get("variable")
    if isinstance(variable, list) or (isinstance(variable, str) and "," in variable):
        errors.append("single_variable_required")
    if exp.get("mode") not in {"replay", "shadow"}:
        errors.append("mode_must_be_replay_or_shadow")
    return errors


def load_experiments(root: Path = PROJECT_ROOT) -> list[dict[str, Any]]:
    path = root / "config" / "experiments.yaml"
    if not path.exists():
        return []
    import yaml

    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = payload.get("experiments") if isinstance(payload, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def experiment_list_report(root: Path = PROJECT_ROOT) -> dict[str, Any]:
    rows = load_experiments(root)
    return {"experiments": [{**row, "validation_errors": validate_experiment(row)} for row in rows]}


def experiment_detail_report(experiment_id: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    for row in load_experiments(root):
        if str(row.get("id")) == str(experiment_id):
            return {**row, "validation_errors": validate_experiment(row), "promotion": "manual_only"}
    return {"id": experiment_id, "status": "not_found"}


def daily_learning_report(*, root: Path = PROJECT_ROOT, day: str, user: str = "live_bot") -> dict[str, Any]:
    audit = run_trading_audit(root=root, day=day, user=user)
    prof = profitability_report(root=root, start=day, end=day, user=user)
    reconciled = audit.ok
    trades = prof["metrics"]["number_of_reconciled_trades"] or 0
    recommendation = "FIX_DATA" if not reconciled else "INSUFFICIENT_DATA" if trades < 30 else "KEEP"
    return {
        "date": day,
        "user": user,
        "data_integrity_status": "ok" if reconciled else "unreconciled",
        "reconciliation_status": "ok" if reconciled else "failed",
        "completed_reconciled_trades": trades,
        "expectancy_by_strategy": prof["breakdowns"].get("strategy_route", {}),
        "underlying_signal_expectancy": "unavailable",
        "option_execution_impact": "unavailable",
        "spread_and_slippage_impact": {
            "estimated_spread_cost": prof["metrics"].get("estimated_spread_cost"),
            "estimated_slippage_cost": prof["metrics"].get("estimated_slippage_cost"),
        },
        "exit_capture_ratio": prof["metrics"].get("mfe_capture_ratio"),
        "runtime_and_data_quality_failures": audit.report.get("integrity_incidents", []),
        "primary_hypothesis": None if recommendation in {"FIX_DATA", "INSUFFICIENT_DATA"} else {"id": f"{day}-{user}-primary", "status": "proposed"},
        "recommendation": recommendation,
    }


def _latest_lifecycle_day(root: Path, user: str) -> str:
    base = root / "data" / "trade_attribution" / "daily"
    days: list[str] = []
    for path in base.glob(f"*_{user}.json"):
        text = path.name.split("_", 1)[0]
        try:
            date.fromisoformat(text)
        except ValueError:
            continue
        days.append(text)
    if not days:
        raise FileNotFoundError(f"no trade attribution days found for {user}")
    return sorted(days)[-1]


def _configured_mode(root: Path) -> str:
    try:
        cfg = load_config(root / "config" / "default.yaml")
    except Exception:
        return "unavailable"
    tc = cfg.get("trading_control") if isinstance(cfg, Mapping) else {}
    return str((tc or {}).get("mode") or "unavailable")


def _configured_strategy_states(root: Path) -> dict[str, str]:
    try:
        cfg = load_config(root / "config" / "default.yaml")
    except Exception:
        cfg = {}
    return {route: row.state for route, row in strategy_states(cfg).items()}


def _effective_strategy_runtime_state(root: Path) -> dict[str, dict[str, Any]]:
    mode = _configured_mode(root)
    configured = _configured_strategy_states(root)
    out: dict[str, dict[str, Any]] = {}
    for route, state in configured.items():
        effective = "ENTRIES_DISABLED" if mode == "entries-disabled" else "SHADOW" if mode == "shadow" and state == "LIVE" else state
        out[route] = {
            "configured_state": state,
            "effective_runtime_state": effective,
            "effective_entry_permission": bool(mode in {"paper", "live"} and state == "LIVE"),
            "hypothetical_entries_allowed": bool(mode == "shadow" and state in {"LIVE", "SHADOW"}),
            "paper_entries_allowed": bool(mode == "paper" and state == "LIVE"),
            "live_entries_allowed": bool(mode == "live" and state == "LIVE"),
            "broker_submission_allowed": bool(mode in {"paper", "live"} and state == "LIVE"),
        }
    return out


def _runtime_permission_summary(root: Path, runtime_progress: Mapping[str, Any] | None = None) -> dict[str, Any]:
    try:
        cfg = load_config(root / "config" / "default.yaml")
        mode_state = resolve_trading_mode(cfg, paper=False, live_operation=True)
    except Exception:
        cfg = {}
        mode_state = None
    strategies = strategy_states(cfg)
    live_routes = {route for route, row in strategies.items() if row.state == "LIVE"}
    shadow_routes = {route for route, row in strategies.items() if row.state == "SHADOW"}
    startup = (runtime_progress or {}).get("startup") if isinstance(runtime_progress, Mapping) else {}
    observed_mode = None
    if isinstance(runtime_progress, Mapping):
        observed_mode = (startup or {}).get("effective_mode") or runtime_progress.get("effective_mode")
    global_live = bool(mode_state and mode_state.live_orders_allowed)
    global_entries = bool(mode_state and mode_state.new_entries_allowed)
    return {
        "global_mode_live_orders_allowed": global_live,
        "global_mode_new_entries_allowed": global_entries,
        "strategy_live_entries_allowed": {route: bool(global_entries and route in live_routes) for route in sorted(strategies)},
        "trend_long_live_entries_allowed": bool(global_entries and "trend_long" in live_routes),
        "shadow_routes_live_entries_allowed": bool(global_entries and any(route in live_routes for route in shadow_routes)),
        "current_cycle_broker_submission_allowed": bool(global_live and global_entries and live_routes),
        "specific_event_broker_submission_allowed": "event_scoped",
        "observed_runtime_mode": observed_mode or "unavailable_from_artifacts",
    }


def _alignment_state(row: Mapping[str, Any]) -> str:
    reason = str(row.get("reason") or row.get("reject_reason") or row.get("entry_quality_reason") or "").lower()
    if reason in {"no_decision", "none"} or "no_decision" in reason:
        return "NO_DECISION"
    if "strategy_route_not_applicable" in reason or "route_not_applicable" in reason:
        return "ROUTE_NOT_APPLICABLE"
    if "incomplete" in reason:
        return "INCOMPLETE"
    if any(token in reason for token in ("missing", "unavailable", "no bars", "bad quote")):
        return "UNAVAILABLE"
    if "stale" in reason:
        return "STALE"
    if "error" in reason or "exception" in reason:
        return "ERROR"
    if "entry_alignment" in reason or "alignment" in reason:
        return "FAIL"
    if row.get("accepted") is True:
        return "PASS"
    return "UNAVAILABLE" if not reason else "FAIL"


def _entry_alignment_summary(candidates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts = Counter(_alignment_state(row) for row in candidates)
    reasons = Counter(str(row.get("reason") or row.get("reject_reason") or row.get("entry_quality_reason") or "unknown") for row in candidates if _alignment_state(row) != "PASS")
    blocked = [row for row in candidates if row.get("accepted") is False]
    return {
        "blocked_decisions": len(blocked),
        "true_alignment_failures": counts.get("FAIL", 0),
        "no_decision": counts.get("NO_DECISION", 0),
        "strategy_route_not_applicable": counts.get("ROUTE_NOT_APPLICABLE", 0),
        "incomplete_evaluations": counts.get("INCOMPLETE", 0),
        "missing_features": counts.get("UNAVAILABLE", 0),
        "stale_features": counts.get("STALE", 0),
        "runtime_errors": counts.get("ERROR", 0),
        "top_failed_subchecks": dict(reasons.most_common(10)),
        "artifact_vs_decision_reconciliation": "available_in_canonical_lifecycle_counts",
        "interpretation": "unmeasurable" if counts.get("UNAVAILABLE", 0) else "strict_or_failed" if counts.get("FAIL", 0) else "passing",
    }


def _avg_signal_report_return(signals: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> float | None:
    values = []
    for row in signals:
        for key in keys:
            value = _safe_float(row.get(key))
            if value is not None:
                values.append(value)
                break
    return _avg(values)


def _forward_unavailable_reason(failure_breakdown: Mapping[str, Any], missing: int) -> str | None:
    if not missing:
        return None
    reasons = [str(reason) for reason, count in failure_breakdown.items() if _safe_int(count) > 0]
    if len(reasons) == 1:
        return "OUTCOME_UNAVAILABLE_" + reasons[0].upper()
    if reasons:
        return "OUTCOME_UNAVAILABLE_EXPLAINED"
    return "OUTCOME_UNAVAILABLE_MISSING_BARS"


def _artifact_runtime_user(root: Path) -> tuple[str | None, str | None]:
    if Path(root).resolve() != PROJECT_ROOT.resolve():
        return None, None
    try:
        st = PROJECT_ROOT.stat()
        user = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        return user, group
    except Exception:
        return "algosphere", "algosphere"


def _load_signal_expectancy_quality(root: Path, day: str) -> dict[str, Any] | None:
    path = root / "data" / "research_metrics" / day / "signal_expectancy_report.json"
    runtime_user, runtime_group = _artifact_runtime_user(root)
    if path.exists() and not artifact_file_readable_by_runtime(path, runtime_user=runtime_user, runtime_group=runtime_group):
        return {
            "scope": "signal_expectancy_report",
            "source": str(path),
            "signals_analyzed": 0,
            "signals_with_valid_forward_bars": 0,
            "missing_bars": 0,
            "backfill_requested": False,
            "backfill_status": "not_requested",
            "unavailable_reason": "OUTCOME_UNAVAILABLE_ARTIFACT_WRITE_PERMISSION_ERROR",
            "lookup_failure_breakdown": {"artifact_write_permission_error": 1},
            "artifact_error": {
                "error_type": "artifact_write_permission_error",
                "generator": "signal_expectancy_report",
                "path": str(path),
                "reason": "artifact_not_readable_by_runtime_user",
                "detail": artifact_target_diagnostics(path, runtime_user=runtime_user, runtime_group=runtime_group),
            },
            "symbols_missing_bars": [],
            "time_buckets_missing_bars": {},
            "source_selected": [],
            "cache_hits": 0,
            "cache_misses": 0,
            "persistence_status": {},
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    dq = payload.get("data_quality") if isinstance(payload.get("data_quality"), Mapping) else {}
    signals = payload.get("signals") if isinstance(payload.get("signals"), list) else []
    missing = _safe_int(dq.get("missing_bars"))
    failure_breakdown = dq.get("lookup_failure_breakdown") if isinstance(dq.get("lookup_failure_breakdown"), Mapping) else {}
    return {
        "scope": "signal_expectancy_report",
        "source": str(path),
        "signals_analyzed": _safe_int(dq.get("signals_analyzed")),
        "signals_with_valid_forward_bars": _safe_int(dq.get("signals_with_valid_forward_bars")),
        "missing_bars": missing,
        "backfill_requested": False,
        "backfill_status": "not_requested",
        "unavailable_reason": _forward_unavailable_reason(failure_breakdown, missing),
        "lookup_success_rate": dq.get("lookup_success_rate"),
        "lookup_failure_breakdown": dict(failure_breakdown),
        "symbols_missing_bars": dq.get("symbols_missing_bars") or dq.get("missing_symbols") or [],
        "symbols_missing_bars_by_reason": dq.get("symbols_missing_bars_by_reason") or {},
        "time_buckets_missing_bars": dq.get("time_buckets_missing_bars") or {},
        "earliest_available_bar": dq.get("earliest_available_bar"),
        "latest_available_bar": dq.get("latest_available_bar"),
        "source_selected": dq.get("source_selected") or [],
        "cache_hits": _safe_int(dq.get("cache_hits")),
        "cache_misses": _safe_int(dq.get("cache_misses")),
        "lookup_latency_ms_avg": dq.get("lookup_latency_ms_avg"),
        "lookup_latency_ms_total": dq.get("lookup_latency_ms_total"),
        "persistence_status": dq.get("persistence_status") or {},
        "average_5m_return": _avg_signal_report_return(signals, ("return_5m_pct", "forward_return_5m")),
        "average_15m_return": _avg_signal_report_return(signals, ("return_15m_pct", "forward_return_15m")),
        "average_30m_return": _avg_signal_report_return(signals, ("return_30m_pct", "forward_return_30m")),
        "average_60m_return": _avg_signal_report_return(signals, ("return_60m_pct", "forward_return_60m")),
        "mfe": _avg_signal_report_return(signals, ("max_favorable_excursion_pct", "mfe_pct")),
        "mae": _avg_signal_report_return(signals, ("max_adverse_excursion_pct", "mae_pct")),
    }


def _forward_outcome_summary(
    candidates: Sequence[Mapping[str, Any]],
    *,
    backfill_bars: bool,
    root: Path | None = None,
    day: str | None = None,
    user: str = "live_bot",
) -> dict[str, Any]:
    if root is not None and day:
        report_quality = _load_signal_expectancy_quality(root, day)
        if report_quality is not None:
            report_quality["backfill_requested"] = bool(backfill_bars)
            report_quality["backfill_status"] = "requested_via_day_review_but_existing_signal_report_used" if backfill_bars else "not_requested"
            return report_quality
        expected_report = root / "data" / "research_metrics" / day / "signal_expectancy_report.json"
        runtime_user, runtime_group = _artifact_runtime_user(root)
        diag = artifact_target_diagnostics(expected_report, runtime_user=runtime_user, runtime_group=runtime_group)
        if not diag.get("target_user_writable") and not diag.get("can_create_directory"):
            return {
                "scope": "signal_expectancy_report",
                "source": str(expected_report),
                "signals_analyzed": 0,
                "signals_with_valid_forward_bars": 0,
                "missing_bars": 0,
                "backfill_requested": bool(backfill_bars),
                "backfill_status": "not_requested",
                "unavailable_reason": "OUTCOME_UNAVAILABLE_ARTIFACT_WRITE_PERMISSION_ERROR",
                "lookup_failure_breakdown": {"artifact_write_permission_error": 1},
                "artifact_error": {
                    "error_type": "artifact_write_permission_error",
                    "generator": "signal_expectancy_report",
                    "path": str(expected_report),
                    "reason": "report_directory_not_writable_by_runtime_user",
                    "detail": diag,
                },
                "symbols_missing_bars": [],
                "time_buckets_missing_bars": {},
                "source_selected": [],
                "cache_hits": 0,
                "cache_misses": 0,
                "persistence_status": {},
            }
        try:
            from src.artifact_writability import ArtifactWriteError
            from src.signal_expectancy_report import write_signal_expectancy_report

            _json_path, _text_path, _report = write_signal_expectancy_report(
                project_root=root,
                data_dir=root / "data",
                day=day,
                user_id=user,
            )
        except ArtifactWriteError as exc:
            return {
                "scope": "signal_expectancy_report",
                "source": str(expected_report),
                "signals_analyzed": 0,
                "signals_with_valid_forward_bars": 0,
                "missing_bars": 0,
                "backfill_requested": bool(backfill_bars),
                "backfill_status": "not_requested",
                "unavailable_reason": "OUTCOME_UNAVAILABLE_ARTIFACT_WRITE_PERMISSION_ERROR",
                "lookup_failure_breakdown": {"artifact_write_permission_error": 1},
                "artifact_error": exc.as_dict(),
                "symbols_missing_bars": [],
                "time_buckets_missing_bars": {},
                "source_selected": [],
                "cache_hits": 0,
                "cache_misses": 0,
                "persistence_status": {},
            }
        except Exception:
            pass
        else:
            report_quality = _load_signal_expectancy_quality(root, day)
            if report_quality is not None:
                report_quality["backfill_requested"] = bool(backfill_bars)
                report_quality["backfill_status"] = "generated_via_day_review"
                return report_quality
    rows_with_forward = [
        row for row in candidates
        if any(row.get(k) is not None for k in ("forward_return_1m", "forward_return_5m", "forward_return_15m", "forward_return_30m", "forward_return_60m", "session_close_return"))
    ]
    missing = len(candidates) - len(rows_with_forward)
    return {
        "scope": "canonical_candidates",
        "source": "canonical_lifecycle",
        "signals_analyzed": len(candidates),
        "signals_with_valid_forward_bars": len(rows_with_forward),
        "missing_bars": missing,
        "backfill_requested": bool(backfill_bars),
        "backfill_status": "not_implemented_without_local_bar_loader" if backfill_bars and missing else "not_requested" if not backfill_bars else "not_needed",
        "unavailable_reason": "OUTCOME_UNAVAILABLE_NO_SIGNAL_EXPECTANCY_REPORT" if missing else None,
        "lookup_failure_breakdown": {"no_signal_expectancy_report": missing} if missing else {},
        "symbols_missing_bars": sorted({_norm_symbol(row.get("symbol")) for row in candidates if row not in rows_with_forward and _norm_symbol(row.get("symbol"))}),
        "time_buckets_missing_bars": {},
        "source_selected": [],
        "cache_hits": 0,
        "cache_misses": 0,
        "persistence_status": {},
        "average_5m_return": _avg([_safe_float(row.get("forward_return_5m")) for row in rows_with_forward]),
        "average_15m_return": _avg([_safe_float(row.get("forward_return_15m")) for row in rows_with_forward]),
        "average_30m_return": _avg([_safe_float(row.get("forward_return_30m")) for row in rows_with_forward]),
        "average_60m_return": _avg([_safe_float(row.get("forward_return_60m")) for row in rows_with_forward]),
        "mfe": _avg([_safe_float(row.get("mfe_pct") or row.get("max_favorable_excursion_pct")) for row in rows_with_forward]),
        "mae": _avg([_safe_float(row.get("mae_pct") or row.get("max_adverse_excursion_pct")) for row in rows_with_forward]),
    }


def _blocked_mode_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    codes = {"ENTRY_BLOCKED_MODE_ENTRIES_DISABLED", "ENTRY_BLOCKED_SHADOW_MODE"}
    out = []
    for row in candidates:
        text = " ".join(str(row.get(k) or "") for k in ("reason", "reject_reason", "block_reason", "entry_quality_reason"))
        if any(code in text for code in codes):
            out.append(row)
    return out


def _hypothetical_summary(candidates: Sequence[Mapping[str, Any]], *, backfill_bars: bool, forward: Mapping[str, Any] | None = None) -> dict[str, Any]:
    blocked = _blocked_mode_candidates(candidates)
    forward_available = bool((forward or {}).get("signals_with_valid_forward_bars", 0))
    if not blocked:
        status = "NO_MODE_BLOCKED_ENTRIES"
        note = "No entries were blocked solely by trading mode, so no blocked-entry simulation was attempted."
    elif any(is_option_symbol(_norm_symbol(row.get("symbol"))) for row in blocked):
        status = "OPTION_SIMULATION_UNAVAILABLE"
        note = "No hypothetical option P&L was fabricated; historical option quotes are required for simulation."
    elif forward_available:
        status = "HYPOTHETICAL_SIMULATION_NOT_AVAILABLE"
        note = "Forward underlying bars are available, but this report did not fabricate execution P&L without explicit fill simulation support."
    else:
        status = (forward or {}).get("unavailable_reason") or "OUTCOME_UNAVAILABLE_MISSING_BARS"
        note = "No hypothetical P&L was fabricated; local bars/quotes are required for simulation."
    return {
        "blocked_by_mode": len(blocked),
        "simulatable": 0,
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "gross_pnl": None,
        "estimated_spread_slippage": None,
        "net_pnl": None,
        "expectancy": None,
        "profit_factor": None,
        "maximum_drawdown": None,
        "median_holding_time": None,
        "mfe_capture_ratio": None,
        "status": status,
        "note": note,
        "backfill_bars": bool(backfill_bars),
    }


def _rejected_signal_control_group(counts: Mapping[str, Any], forward: Mapping[str, Any]) -> dict[str, Any]:
    approved = counts.get("approved_decisions") or counts.get("approved_entries")
    rejected = counts.get("blocked_decisions") or counts.get("blocked_entries")
    if _safe_int(forward.get("signals_with_valid_forward_bars")) <= 0:
        return {
            "status": "INSUFFICIENT_DATA",
            "approved_count": approved,
            "rejected_count": rejected,
            "conclusion": "approval-filter value cannot be measured without forward bars",
        }
    return {
        "status": "FORWARD_OUTCOMES_AVAILABLE",
        "approved_count": approved,
        "rejected_count": rejected,
        "conclusion": "forward outcomes are available; use signal-quality route and symbol tables for filter review",
    }


def _single_priority(status_name: str | None, counts: Mapping[str, Any], forward: Mapping[str, Any]) -> str:
    if _safe_int(forward.get("signals_with_valid_forward_bars")) <= 0:
        return "Capture or load local forward bars for the reviewed trading date before evaluating signal edge."
    if status_name in {"CONTAMINATED", "UNRECONCILED"}:
        return "Fix lifecycle reconciliation before using profitability for strategy decisions."
    if _safe_int(counts.get("unique_fills")) == 0:
        return "Use restored forward outcomes to evaluate signal edge while entries remain disabled."
    return "Review reconciled expectancy and execution quality before any manual promotion."


def _strategy_breakdown(day: Mapping[str, Any], profitability: Mapping[str, Any]) -> dict[str, Any]:
    by_route: dict[str, dict[str, Any]] = defaultdict(lambda: {"decisions": 0, "approved": 0, "blocked": 0, "simulatable": 0})
    for row in day.get("candidates") or []:
        route = str(row.get("route") or row.get("source") or "unknown")
        by_route[route]["decisions"] += 1
        if row.get("accepted") is True:
            by_route[route]["approved"] += 1
        elif row.get("accepted") is False:
            by_route[route]["blocked"] += 1
    prof_by_route = ((profitability.get("breakdowns") or {}).get("strategy_route") or {}) if isinstance(profitability, Mapping) else {}
    out: dict[str, Any] = {}
    for route, row in sorted(by_route.items()):
        raw = prof_by_route.get(route) if isinstance(prof_by_route, Mapping) else None
        out[route] = {
            **row,
            "net_hypothetical_expectancy": None,
            "realized_reconciled_expectancy": None if (profitability.get("unreconciled_days") if isinstance(profitability, Mapping) else True) else (raw or {}).get("expectancy"),
            "raw_unreconciled_expectancy": (raw or {}).get("expectancy") if isinstance(raw, Mapping) else None,
            "data_status": "unreconciled" if profitability.get("unreconciled_days") else "ok",
            "recommendation": "FIX_DATA" if profitability.get("unreconciled_days") else "INSUFFICIENT_DATA",
        }
    return out


def day_review_report(
    *,
    root: Path = PROJECT_ROOT,
    day: str,
    user: str,
    backfill_bars: bool = False,
    include_rejected: bool = False,
    strategy: str | None = None,
    symbol: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    if day == "latest":
        day = _latest_lifecycle_day(root, user)
    canonical = build_canonical_day(root=root, day=day, user_id=user, mode=mode, symbol=symbol, strategy=strategy)
    audit = run_trading_audit(root=root, day=day, user=user)
    profitability = profitability_report(root=root, start=day, end=day, user=user, mode=mode, strategy=strategy)
    news = news_edge_report(root=root, start=day, end=day)
    candidates = canonical["candidates"]
    forward = _forward_outcome_summary(candidates, backfill_bars=backfill_bars, root=root, day=day, user=user)
    alignment = _entry_alignment_summary(candidates)
    hypothetical = _hypothetical_summary(candidates, backfill_bars=backfill_bars, forward=forward)
    if hypothetical.get("status", "").startswith("OUTCOME_UNAVAILABLE"):
        hypothetical["lookup_failure_breakdown"] = forward.get("lookup_failure_breakdown") or {}
    quarantine = quarantine_candidates(canonical)
    incidents = _load_integrity_incidents(root, user, day=day)
    recent_incidents = incidents[-20:]
    status = audit.report.get("integrity_status") or canonical.get("integrity_status")
    status_name = (status or {}).get("status")
    runtime_progress = load_runtime_progress(root / "data", day=day, user_id=user)
    runtime_startup = (runtime_progress or {}).get("startup") if isinstance(runtime_progress, Mapping) else {}
    observed_runtime_mode = mode or (runtime_startup or {}).get("effective_mode") or (runtime_progress or {}).get("effective_mode") or "unavailable_from_artifacts"
    permission_summary = _runtime_permission_summary(root, runtime_progress if isinstance(runtime_progress, Mapping) else None)
    session_activity = summarize_session_activity(
        runtime_progress,
        shadow_intents_current_day=int((canonical.get("counts") or {}).get("shadow_order_intents") or 0),
    )
    if status_name in {"CONTAMINATED", "UNRECONCILED"}:
        recommendation = "FIX_DATA"
        confidence = "high"
        reason = "Canonical lifecycle evidence is contaminated or unreconciled, so profitability is not trustworthy."
    elif forward.get("signals_with_valid_forward_bars", 0) == 0:
        recommendation = "INSUFFICIENT_DATA"
        confidence = "medium"
        reason = "Lifecycle is available but forward outcomes are missing."
    else:
        recommendation = "CONTINUE_SHADOW"
        confidence = "low"
        reason = "Forward outcomes exist but promotion remains manual."
    counts = {**canonical["counts"], **audit.report["counts"]}
    report = {
        "date": day,
        "user": user,
        "scope": canonical["scope"],
        "executive_decision": {
            "trading_recommendation": recommendation,
            "confidence": confidence,
            "primary_reason": reason,
        },
        "system_and_safety_state": {
            "configured_trading_mode": _configured_mode(root),
            "observed_runtime_mode": observed_runtime_mode,
            "live_broker_orders_allowed": permission_summary["global_mode_live_orders_allowed"],
            "new_entries_allowed": permission_summary["global_mode_new_entries_allowed"],
            **permission_summary,
            "runtime_progress": runtime_progress,
            "session_activity": session_activity,
            "strategy_states": _configured_strategy_states(root),
            "effective_strategy_runtime_state": _effective_strategy_runtime_state(root),
            "runtime_exceptions": recent_incidents,
            "runtime_exception_count": len(incidents),
            "integrity_incidents": recent_incidents,
            "integrity_incident_count": len(incidents),
            "replay_contamination": counts.get("synthetic_or_replay_order_events", 0),
            "startup_validation_status": "available" if runtime_startup else "unavailable",
        },
        "data_integrity": {
            "lifecycle_status": status,
            "raw_vs_unique_counts": counts,
            "replay_mock_records": counts.get("synthetic_or_replay_order_events", 0),
            "replay_research_outcomes": counts.get("replay_research_outcomes", 0),
            "duplicate_replay_research_outcomes": counts.get("duplicate_replay_research_outcomes", 0),
            "attribution_snapshots": counts.get("attribution_snapshots", 0),
            "duplicate_attribution_snapshots": counts.get("duplicate_attribution_snapshots", 0),
            "contaminated_fill_events": counts.get("contaminated_fill_events", 0),
            "ambiguous_unresolved_records": counts.get("ambiguous_unresolved_records", 0),
            "unresolved_contamination": counts.get("unresolved_contamination", 0),
            "orphan_fill_events": counts.get("orphan_fill_events", 0),
            "duplicate_records": {
                "orders": len(canonical.get("duplicate_order_sources") or []),
                "shadow_intents": len(audit.report.get("duplicate_shadow_rows") or []),
                "fills": len(canonical.get("duplicate_fill_sources") or []),
            },
            "missing_bars": forward.get("missing_bars"),
            "missing_features": alignment.get("missing_features"),
            "missing_news_links": (news.get("comparisons") or {}).get("status"),
            "missing_option_quotes": "unavailable",
            "profitability_trustworthy": not profitability.get("unreconciled_days") and status_name in {"CLEAN", "PARTIAL"},
            "quarantine_candidates": quarantine[:20],
            "quarantine_candidate_count": len(quarantine),
        },
        "market_context": {
            "spy_direction": "unavailable",
            "qqq_direction": "unavailable",
            "volatility_regime": "unavailable",
            "trend_range_classification": "unavailable",
            "sector_leadership": "unavailable",
            "market_open_gap": "unavailable",
            "major_news_events": len(news.get("events") or []),
        },
        "canonical_funnel": counts,
        "shadow_lifecycle": {
            "shadow_decisions": counts.get("shadow_decisions", 0),
            "shadow_selected_candidates": counts.get("shadow_allocator_actions", 0),
            "shadow_allocator_actions": counts.get("shadow_allocator_actions", 0),
            "shadow_order_intents": counts.get("shadow_order_intents", 0),
            "shadow_execution_blocks": counts.get("shadow_execution_blocks", 0),
            "legacy_shadow_records_reclassified": counts.get("legacy_shadow_records_reclassified", 0),
            "duplicate_shadow_intent_events": counts.get("duplicate_shadow_intent_events", 0),
            "broker_dispatch_attempted_count": counts.get("real_order_submission_attempts", 0),
            "execution_allowed_count": counts.get("real_broker_accepted_orders", 0),
            "hypothetical_notional": sum(
                _safe_float(row.get("notional")) or 0.0
                for row in audit.report.get("shadow_order_rows", [])
                if _is_shadow_row(row)
            ),
            "symbols": sorted(
                {
                    _norm_symbol(row.get("symbol"))
                    for row in audit.report.get("shadow_order_rows", [])
                    if _norm_symbol(row.get("symbol"))
                }
            ),
            "routes": sorted(
                {
                    str(row.get("route") or row.get("source") or "")
                    for row in audit.report.get("shadow_order_rows", [])
                    if str(row.get("route") or row.get("source") or "")
                }
            ),
        },
        "signal_quality": forward,
        "hypothetical_performance": hypothetical,
        "strategy_breakdown": _strategy_breakdown(canonical, profitability),
        "top_signals": _top_signals(candidates, include_rejected=include_rejected),
        "rejected_signal_control_group": _rejected_signal_control_group(counts, forward),
        "entry_alignment_analysis": alignment,
        "news_analysis": {
            "events": len(news.get("events") or []),
            "providers": news.get("provider_counts") or {},
            "linked_signals": 0,
            "linked_hypothetical_trades": 0,
            "recommendation": news.get("recommendation"),
            "status": (news.get("comparisons") or {}).get("status"),
        },
        "execution_analysis": {
            "signal_to_order_latency": None,
            "signal_to_fill_latency": None,
            "quote_age": None,
            "spread": None,
            "slippage": None,
            "rejected_orders": counts.get("rejected_orders"),
            "broker_acceptance": counts.get("unique_broker_accepted_orders"),
            "fill_rate": None,
            "option_data_availability": "unavailable",
        },
        "exit_analysis": {
            "stop_loss_outcomes": "unavailable",
            "take_profit_outcomes": "unavailable",
            "trailing_stop_outcomes": "unavailable",
            "time_exits": "unavailable",
            "eod_flatten": "unavailable",
            "mfe_capture": profitability.get("metrics", {}).get("mfe_capture_ratio"),
            "peak_giveback": "unavailable",
            "premature_exits": "unavailable",
            "late_stops": "unavailable",
        },
        "open_positions": {
            "current_day_lifecycle_positions": canonical["position_state"].get("open", {}),
            "broker_reported_positions": "unavailable_without_broker_query",
            "lineage_warnings": ["BROKER_POSITION_MISSING_LOCAL_LINEAGE requires broker position snapshot"] if counts.get("unique_still_open_positions") == 0 else [],
        },
        "what_worked": _what_worked(canonical, forward),
        "what_failed": _what_failed(status_name, counts, forward, alignment),
        "single_priority_for_next_session": _single_priority(status_name, counts, forward),
        "what_not_to_change": [
            "Do not enable additional live strategies.",
            "Do not loosen entry alignment or adaptive thresholds without reconciled evidence.",
            "Do not treat replay/mock fills as live broker fills.",
        ],
        "final_readiness_gate": {
            "lifecycle_clean": status_name in {"CLEAN", "PARTIAL"},
            "replay_contamination_zero": counts.get("synthetic_or_replay_order_events", 0) == 0,
            "reconciled_trades_available": profitability.get("metrics", {}).get("number_of_reconciled_trades", 0) > 0,
            "forward_outcomes_available": forward.get("signals_with_valid_forward_bars", 0) > 0,
            "strategy_expectancy_positive": False,
            "startup_validation_healthy": "unavailable",
            "safe_to_move_entries_disabled_to_shadow": status_name in {"CLEAN", "PARTIAL"},
            "safe_to_move_shadow_to_paper": False,
            "safe_to_move_paper_to_limited_live": False,
        },
        "source_inventory": canonical["sources"],
    }
    return report


def _top_signals(candidates: Sequence[Mapping[str, Any]], *, include_rejected: bool) -> list[dict[str, Any]]:
    rows = candidates if include_rejected else [row for row in candidates if row.get("accepted") is True]
    out = []
    for row in rows[:10]:
        out.append(
            {
                "time": row.get("timestamp"),
                "symbol": row.get("symbol"),
                "route": row.get("route") or row.get("source"),
                "approved_or_rejected": "approved" if row.get("accepted") is True else "rejected" if row.get("accepted") is False else "unknown",
                "block_reason": row.get("reason") or row.get("reject_reason") or row.get("entry_quality_reason"),
                "signal_price": _safe_float(row.get("price") or row.get("signal_price")),
                "hypothetical_fill": None,
                "return_15m": _safe_float(row.get("forward_return_15m")),
                "return_30m": _safe_float(row.get("forward_return_30m")),
                "mfe": _safe_float(row.get("mfe_pct") or row.get("max_favorable_excursion_pct")),
                "mae": _safe_float(row.get("mae_pct") or row.get("max_adverse_excursion_pct")),
                "hypothetical_outcome": "unavailable",
                "exit_reason": None,
                "data_quality_notes": "forward bars unavailable" if row.get("forward_return_15m") is None else "available",
            }
        )
    return out


def _what_worked(day: Mapping[str, Any], forward: Mapping[str, Any]) -> list[str]:
    out = []
    if int((day.get("counts") or {}).get("unique_entry_decisions") or 0) > 0:
        out.append("Canonical decision records were present for the day.")
    if int(forward.get("signals_with_valid_forward_bars") or 0) > 0:
        out.append("Some forward outcomes were measurable.")
    return out[:3] or ["No evidence-backed positives were available."]


def _what_failed(status_name: str | None, counts: Mapping[str, Any], forward: Mapping[str, Any], alignment: Mapping[str, Any]) -> list[str]:
    out = []
    if status_name == "CONTAMINATED":
        out.append("Replay/mock lifecycle records contaminated live evidence.")
    if int(forward.get("missing_bars") or 0) > 0:
        out.append("Forward bars were missing for signal expectancy.")
    if int(alignment.get("missing_features") or 0) > 0:
        out.append("Some alignment decisions lacked persisted feature evidence.")
    if int(counts.get("duplicate_fill_events") or 0) > 0:
        out.append("Raw fill snapshots contained duplicates and had to be collapsed.")
    return out[:3] or ["No evidence-backed failures were identified."]


def _row_identity_forensics(src: SourceRow, *, user: str, duplicate_kind: str) -> dict[str, Any]:
    row = src.row
    normalized_ts = normalize_lifecycle_timestamps(row)
    raw_key = _norm_id(row.get("order_id") or row.get("broker_order_id") or row.get("client_order_id") or row.get("logical_order_id"))
    fill_id = _explicit_fill_id(row)
    recommended_fill_key = (
        f"broker_activity_id:{fill_id}"
        if fill_id
        else f"composite:{canonical_order_key(row)}:{_norm_symbol(row.get('option_symbol') or row.get('symbol'))}:{_row_side(row)}:{normalized_ts['event_timestamp_utc']}:{_safe_float(row.get('filled_qty'))}:{_safe_float(row.get('filled_avg_price') or row.get('fill_price') or row.get('price'))}"
    )
    shadow_reason = shadow_reclassification_reason(row)
    order_identifier = row.get("broker_order_id") or row.get("order_id")
    return {
        "duplicate_kind": duplicate_kind,
        "record_source": src.source,
        "file_path_or_table": src.path,
        "record_index_or_primary_key": src.index,
        "broker_event_timestamp": normalized_ts["event_timestamp_utc"],
        "local_persisted_timestamp": row.get("timestamp"),
        "event_trading_date_et": normalized_ts["event_trading_date_et"],
        "broker_order_id": None if shadow_reason else order_identifier,
        "shadow_intent_id": order_identifier if shadow_reason else None,
        "client_order_id": row.get("client_order_id"),
        "broker_activity_fill_id": row.get("broker_activity_id") or row.get("activity_id") or row.get("fill_id") or row.get("execution_id"),
        "symbol": row.get("underlying_symbol") or row.get("symbol"),
        "option_symbol": row.get("option_symbol") if row.get("option_symbol") else (row.get("symbol") if is_option_symbol(_norm_symbol(row.get("symbol"))) else None),
        "side": _row_side(row),
        "quantity": _safe_float(row.get("qty") or row.get("quantity")),
        "filled_quantity": _safe_float(row.get("filled_qty")),
        "price": _safe_float(row.get("filled_avg_price") or row.get("fill_price") or row.get("price")),
        "status": row.get("status"),
        "strategy_route": row.get("route") or row.get("entry_route"),
        "decision_id": row.get("decision_id"),
        "logical_order_id": row.get("logical_order_id"),
        "position_id": canonical_position_key(row, user=user),
        "raw_identity_key_currently_used": raw_key,
        "recommended_canonical_identity_key": canonical_order_key(row) if duplicate_kind == "order" else recommended_fill_key,
        "synthetic_or_replay": _is_synthetic_or_replay(row),
        "shadow_expected": bool(shadow_reason),
        "shadow_reclassification_reason": shadow_reason,
    }


def duplicate_forensics_report(
    *,
    root: Path = PROJECT_ROOT,
    day: str,
    user: str,
    order_id: str | None = None,
    fill_id: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    state = build_lifecycle_state(root=root, day=day, user=user)
    target_order = _norm_id(order_id)
    target_fill = _norm_id(fill_id)
    target_symbol = _norm_symbol(symbol)

    def include(src: SourceRow) -> bool:
        row = src.row
        if target_order and target_order not in {
            _norm_id(row.get("order_id")),
            _norm_id(row.get("broker_order_id")),
            _norm_id(row.get("client_order_id")),
            _norm_id(row.get("logical_order_id")),
            canonical_order_key(row),
        }:
            return False
        if target_fill and target_fill not in {
            _explicit_fill_id(row),
            _norm_id(row.get("order_id")),
            canonical_order_key(row),
        }:
            return False
        if target_symbol and target_symbol not in {_norm_symbol(row.get("symbol")), _norm_symbol(row.get("option_symbol")), _norm_symbol(row.get("underlying_symbol"))}:
            return False
        return True

    duplicate_order_sources = [src for src in state["duplicate_order_sources"] if include(src)]
    order_groups: dict[str, list[SourceRow]] = defaultdict(list)
    for src in duplicate_order_sources:
        order_groups[canonical_order_key(src.row)].append(src)
    shadow_groups = {
        k: [src for src in rows if include(src)]
        for k, rows in _unique_orders(state.get("shadow_order_sources") or []).items()
        if len(rows) > 1
    }
    shadow_groups = {k: rows for k, rows in shadow_groups.items() if rows}
    replay_groups = {
        k: [src for src in rows if include(src)]
        for k, rows in _unique_orders([src for src in state["order_sources"] if _is_synthetic_or_replay(src.row)]).items()
        if len(rows) > 1
    }
    replay_groups = {k: rows for k, rows in replay_groups.items() if rows}
    fill_duplicate_sources = [src for src in state["duplicate_fill_sources"] if include(src)]
    fill_groups: dict[str, list[SourceRow]] = defaultdict(list)
    for src in fill_duplicate_sources:
        fill_groups[_explicit_fill_id(src.row) or canonical_order_key(src.row)].append(src)
    duplicate_orders = []
    for key, rows in sorted(order_groups.items()):
        encoded = [json.dumps(src.row, sort_keys=True, default=str) for src in rows]
        filled_qty_values = [_safe_float(src.row.get("filled_qty")) or 0.0 for src in rows]
        duplicate_orders.append(
            {
                "canonical_key": key,
                "record_count": len(rows),
                "records": [_row_identity_forensics(src, user=user, duplicate_kind="order") for src in rows],
                "byte_identical": len(set(encoded)) == 1,
                "cumulative_snapshots": filled_qty_values == sorted(filled_qty_values) and len(set(filled_qty_values)) > 1,
                "separate_persistence_sources": len({src.source for src in rows}) > 1,
            }
        )
    duplicate_fills = []
    for key, rows in sorted(fill_groups.items()):
        encoded = [json.dumps(src.row, sort_keys=True, default=str) for src in rows]
        filled_qty_values = [_safe_float(src.row.get("filled_qty")) or 0.0 for src in rows]
        duplicate_fills.append(
            {
                "canonical_key": key,
                "record_count": len(rows),
                "records": [_row_identity_forensics(src, user=user, duplicate_kind="fill") for src in rows],
                "byte_identical": len(set(encoded)) == 1,
                "cumulative_snapshots": filled_qty_values == sorted(filled_qty_values) and len(set(filled_qty_values)) > 1,
                "separate_persistence_sources": len({src.source for src in rows}) > 1,
            }
        )
    duplicate_shadow_intents = []
    for key, rows in sorted(shadow_groups.items()):
        encoded = [json.dumps(src.row, sort_keys=True, default=str) for src in rows]
        duplicate_shadow_intents.append(
            {
                "canonical_key": key,
                "record_count": len(rows),
                "records": [_row_identity_forensics(src, user=user, duplicate_kind="shadow_intent") for src in rows],
                "byte_identical": len(set(encoded)) == 1,
                "separate_persistence_sources": len({src.source for src in rows}) > 1,
            }
        )
    duplicate_replay_orders = []
    for key, rows in sorted(replay_groups.items()):
        encoded = [json.dumps(src.row, sort_keys=True, default=str) for src in rows]
        duplicate_replay_orders.append(
            {
                "canonical_key": key,
                "record_count": len(rows),
                "records": [_row_identity_forensics(src, user=user, duplicate_kind="replay_mock_order") for src in rows],
                "byte_identical": len(set(encoded)) == 1,
                "separate_persistence_sources": len({src.source for src in rows}) > 1,
            }
        )
    return {
        "date": day,
        "user": user,
        "scope": {
            "name": "duplicate_lifecycle_forensics",
            "source": str(state["path"]),
            "date_basis": "event_timestamp_america_new_york",
            "environment_filter": "selected user",
            "record_origin_filter": "all origins shown; replay/mock/shadow identified",
        },
        "filters": {"order_id": order_id, "fill_id": fill_id, "symbol": symbol},
        "counts": state["counts"],
        "integrity_status": lifecycle_status(state["counts"], []),
        "duplicate_orders": duplicate_orders,
        "duplicate_shadow_intents": duplicate_shadow_intents,
        "duplicate_replay_orders": duplicate_replay_orders,
        "duplicate_fills": duplicate_fills,
        "authoritative_sources": state["sources"],
        "cause_summary": _duplicate_cause_summary(state),
    }


def _duplicate_cause_summary(state: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    synthetic = int((state.get("counts") or {}).get("synthetic_or_replay_order_events") or 0)
    if synthetic:
        out.append(f"{synthetic} synthetic/replay order events are present in the live attribution artifact.")
    if state.get("duplicate_order_ids"):
        out.append("Repeated order status snapshots share the same canonical order identity and must not be counted as separate submitted orders.")
    if state.get("duplicate_shadow_ids"):
        out.append("Repeated shadow intent snapshots are tracked separately from real submitted broker orders.")
    replay_duplicates = _unique_orders([src for src in state.get("order_sources", []) if _is_synthetic_or_replay(src.row)])
    if any(len(rows) > 1 for rows in replay_duplicates.values()):
        out.append("Repeated replay/mock order snapshots are tracked separately from real submitted broker orders.")
    if state.get("duplicate_fill_sources"):
        out.append("Repeated or cumulative fill snapshots share one fill lineage and must not create duplicate positions.")
    if not out:
        out.append("No duplicate lifecycle rows detected with the selected filters.")
    return out


def render_audit_md(report: Mapping[str, Any]) -> str:
    lines = [f"# Trading Audit {report.get('date')} {report.get('user')}", "", f"- Fully reconciled: {str(report.get('fully_reconciled')).lower()}", ""]
    lines.append("## Counts")
    for k, v in (report.get("counts") or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Problems")
    problems = report.get("problems") or []
    if not problems:
        lines.append("- none")
    else:
        for p in problems:
            lines.append(f"- {p.get('kind')}: {p.get('detail')}")
    return "\n".join(lines) + "\n"


def render_duplicate_forensics_md(report: Mapping[str, Any]) -> str:
    lines = [f"# Duplicate Forensics {report.get('date')} {report.get('user')}", ""]
    lines.append("## Cause Summary")
    for item in report.get("cause_summary") or []:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Counts")
    for key in (
        "raw_submitted_order_events",
        "unique_submitted_orders",
        "raw_fill_events",
        "unique_fills",
        "duplicate_fill_events",
        "raw_position_records",
        "unique_opened_positions",
        "unique_closed_positions",
        "unique_still_open_positions",
        "synthetic_or_replay_order_events",
        "unresolved_contamination",
        "replay_research_outcomes",
        "duplicate_replay_research_outcomes",
        "attribution_snapshots",
        "duplicate_attribution_snapshots",
        "ambiguous_unresolved_records",
        "shadow_order_intents",
        "duplicate_shadow_intent_events",
        "legacy_shadow_records_reclassified",
    ):
        lines.append(f"- {key}: {(report.get('counts') or {}).get(key)}")
    lines.append("")
    lines.append("## Duplicate Orders")
    if not report.get("duplicate_orders"):
        lines.append("- none")
    for group in report.get("duplicate_orders") or []:
        lines.append(
            f"- {group.get('canonical_key')}: records={group.get('record_count')} "
            f"byte_identical={str(group.get('byte_identical')).lower()} "
            f"cumulative_snapshots={str(group.get('cumulative_snapshots')).lower()} "
            f"separate_sources={str(group.get('separate_persistence_sources')).lower()}"
        )
    lines.append("")
    lines.append("## Duplicate Shadow Intents")
    if not report.get("duplicate_shadow_intents"):
        lines.append("- none")
    for group in report.get("duplicate_shadow_intents") or []:
        lines.append(
            f"- {group.get('canonical_key')}: records={group.get('record_count')} "
            f"byte_identical={str(group.get('byte_identical')).lower()} "
            f"separate_sources={str(group.get('separate_persistence_sources')).lower()}"
        )
    lines.append("")
    lines.append("## Duplicate Replay/Mock Orders")
    if not report.get("duplicate_replay_orders"):
        lines.append("- none")
    for group in report.get("duplicate_replay_orders") or []:
        lines.append(
            f"- {group.get('canonical_key')}: records={group.get('record_count')} "
            f"byte_identical={str(group.get('byte_identical')).lower()} "
            f"separate_sources={str(group.get('separate_persistence_sources')).lower()}"
        )
    lines.append("")
    lines.append("## Duplicate Fills")
    if not report.get("duplicate_fills"):
        lines.append("- none")
    for group in report.get("duplicate_fills") or []:
        lines.append(
            f"- {group.get('canonical_key')}: records={group.get('record_count')} "
            f"byte_identical={str(group.get('byte_identical')).lower()} "
            f"cumulative_snapshots={str(group.get('cumulative_snapshots')).lower()} "
            f"separate_sources={str(group.get('separate_persistence_sources')).lower()}"
        )
    return "\n".join(lines) + "\n"


def render_day_review_md(report: Mapping[str, Any]) -> str:
    day = report.get("date")
    lines = [f"# AlgoSphere Trading Day Review — {day}", ""]
    decision = report.get("executive_decision") if isinstance(report.get("executive_decision"), Mapping) else {}
    lines.extend(
        [
            "## Executive Decision",
            "",
            "Trading recommendation:",
            f"- {decision.get('trading_recommendation')}",
            "",
            "Confidence:",
            f"- {decision.get('confidence')}",
            "",
            "Primary reason:",
            str(decision.get("primary_reason") or "unavailable"),
            "",
        ]
    )

    def section(title: str) -> None:
        lines.extend([f"## {title}", ""])

    def bullets(mapping: Mapping[str, Any], keys: Sequence[str] | None = None) -> None:
        for key in keys or list(mapping):
            value = mapping.get(key)
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True, default=str)
            lines.append(f"- {key}: {value}")
        lines.append("")

    section("System and Safety State")
    bullets(report.get("system_and_safety_state") or {})
    section("Data Integrity")
    bullets(report.get("data_integrity") or {})
    section("Market Context")
    bullets(report.get("market_context") or {})
    section("Canonical Funnel")
    funnel = report.get("canonical_funnel") or {}
    bullets(
        funnel,
        [
            "raw_scanner_events",
            "unique_scanner_events",
            "selected_candidates",
            "unique_entry_decisions",
            "approved_decisions",
            "blocked_decisions",
            "allocator_actions",
            "unique_broker_accepted_orders",
            "unique_fills",
            "unique_opened_positions",
            "unique_closed_positions",
            "unique_still_open_positions",
            "synthetic_or_replay_order_events",
        ],
    )
    lines.append("Discrepancies are explained by raw loop/snapshot rows being collapsed into canonical decision, order, fill, and position identities.")
    lines.append("")
    section("Signal Quality")
    bullets(report.get("signal_quality") or {})
    section("Hypothetical Performance")
    bullets(report.get("hypothetical_performance") or {})
    section("Strategy Breakdown")
    for route, row in (report.get("strategy_breakdown") or {}).items():
        lines.append(f"- {route}: {json.dumps(row, sort_keys=True, default=str)}")
    lines.append("")
    section("Top Signals")
    top = report.get("top_signals") or []
    if not top:
        lines.append("- none")
    for row in top[:10]:
        lines.append(f"- {row.get('time')} {row.get('symbol')} {row.get('route')} {row.get('approved_or_rejected')} reason={row.get('block_reason')}")
    lines.append("")
    section("Rejected-Signal Control Group")
    bullets(report.get("rejected_signal_control_group") or {})
    section("Entry Alignment Analysis")
    bullets(report.get("entry_alignment_analysis") or {})
    section("News Analysis")
    bullets(report.get("news_analysis") or {})
    section("Execution Analysis")
    bullets(report.get("execution_analysis") or {})
    section("Exit Analysis")
    bullets(report.get("exit_analysis") or {})
    section("Open Positions")
    bullets(report.get("open_positions") or {})
    section("What Worked")
    for item in report.get("what_worked") or []:
        lines.append(f"- {item}")
    lines.append("")
    section("What Failed")
    for item in report.get("what_failed") or []:
        lines.append(f"- {item}")
    lines.append("")
    section("Single Priority for Next Session")
    lines.extend([str(report.get("single_priority_for_next_session") or "unavailable"), ""])
    section("What Not to Change")
    for item in report.get("what_not_to_change") or []:
        lines.append(f"- {item}")
    lines.append("")
    section("Final Readiness Gate")
    bullets(report.get("final_readiness_gate") or {})
    return "\n".join(lines) + "\n"


def render_generic_md(title: str, report: Mapping[str, Any]) -> str:
    return f"# {title}\n\n```json\n{json.dumps(report, indent=2, sort_keys=True, default=str)}\n```\n"


def render_news_edge_md(report: Mapping[str, Any]) -> str:
    lines = [f"# News Edge Report {report.get('from')} to {report.get('to')}", ""]
    lines.append(f"- Recommendation: {report.get('recommendation')}")
    lines.append(f"- Events: {len(report.get('events') or [])}")
    lines.append(f"- Incremental expectancy: {(report.get('comparisons') or {}).get('incremental_expectancy')}")
    lines.append(f"- Status: {(report.get('comparisons') or {}).get('status')}")
    lines.append("")
    lines.append("## Providers")
    providers = report.get("provider_counts") or {}
    if providers:
        for provider, count in sorted(providers.items()):
            lines.append(f"- {provider}: {count}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Data Gaps")
    lines.append("- Reconciled article-to-trade links are required before news can increase entry eligibility.")
    lines.append("- Historical ingestion alone is not treated as predictive value.")
    return "\n".join(lines) + "\n"


def write_report(kind: str, name: str, report: Mapping[str, Any], *, md_title: str | None = None) -> tuple[Path, Path]:
    base = REPORT_ROOT / kind
    json_path = base / f"{name}.json"
    md_path = base / f"{name}.md"
    atomic_write(json_path, json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    if kind == "trading_audit":
        atomic_write(md_path, render_audit_md(report))
    elif kind == "day_review":
        atomic_write(md_path, render_day_review_md(report))
    elif kind == "duplicate_forensics":
        atomic_write(md_path, render_duplicate_forensics_md(report))
    elif kind == "news_edge":
        atomic_write(md_path, render_news_edge_md(report))
    else:
        atomic_write(md_path, render_generic_md(md_title or kind, report))
    return json_path, md_path


def trading_audit_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--broker", choices=["alpaca"], default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_trading_audit(root=PROJECT_ROOT, day=args.date, user=args.user, broker=args.broker)
        write_report("trading_audit", args.date, result.report)
        print(json.dumps(result.report, indent=2, sort_keys=True) if args.json else render_audit_md(result.report), end="")
        return 0 if result.ok else 1
    except Exception as exc:
        print(f"trading-audit failed: {type(exc).__name__}: {exc}")
        return 2


def duplicate_forensics_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--order-id")
    parser.add_argument("--fill-id")
    parser.add_argument("--symbol")
    args = parser.parse_args(argv)
    try:
        report = duplicate_forensics_report(
            root=PROJECT_ROOT,
            day=args.date,
            user=args.user,
            order_id=args.order_id,
            fill_id=args.fill_id,
            symbol=args.symbol,
        )
        write_report("duplicate_forensics", args.date, report, md_title="Duplicate Forensics")
        print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else render_duplicate_forensics_md(report), end="")
        return 0
    except Exception as exc:
        print(f"duplicate-forensics failed: {type(exc).__name__}: {exc}")
        return 2


def day_review_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--backfill-bars", action="store_true")
    parser.add_argument("--include-rejected", action="store_true")
    parser.add_argument("--strategy")
    parser.add_argument("--symbol")
    parser.add_argument("--mode", choices=["live", "paper", "shadow"])
    args = parser.parse_args(argv)
    try:
        report = day_review_report(
            root=PROJECT_ROOT,
            day=args.date,
            user=args.user,
            backfill_bars=args.backfill_bars,
            include_rejected=args.include_rejected,
            strategy=args.strategy,
            symbol=args.symbol,
            mode=args.mode,
        )
        write_report("day_review", str(report["date"]), report, md_title="Daily Trading Review")
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        else:
            print(render_day_review_md(report), end="")
        status = ((report.get("data_integrity") or {}).get("lifecycle_status") or {}).get("status")
        if status in {"CLEAN", "PARTIAL"}:
            return 0
        return 1
    except Exception as exc:
        print(f"day-review failed: {type(exc).__name__}: {exc}")
        return 2


def profitability_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strategy")
    parser.add_argument("--mode", choices=["live", "paper", "shadow"])
    args = parser.parse_args(argv)
    try:
        report = profitability_report(root=PROJECT_ROOT, start=args.start, end=args.end, user=args.user, mode=args.mode, strategy=args.strategy)
        write_report("profitability", f"{args.start}_{args.end}_{args.user}", report, md_title="Profitability Report")
        print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else render_generic_md("Profitability Report", report), end="")
        return 1 if report.get("unreconciled_days") else 0
    except Exception as exc:
        print(f"profitability-report failed: {type(exc).__name__}: {exc}")
        return 2


def news_edge_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = news_edge_report(root=PROJECT_ROOT, start=args.start, end=args.end)
        write_report("news_edge", f"{args.start}_{args.end}", report, md_title="News Edge Report")
        print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else render_news_edge_md(report), end="")
        return 0
    except Exception as exc:
        print(f"news-edge-report failed: {type(exc).__name__}: {exc}")
        return 2


def strategy_readiness_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        from src.config_loader import load_config

        report = strategy_readiness_report(root=PROJECT_ROOT, config=load_config(), user=args.user)
        print(json.dumps(report, indent=2, sort_keys=True, default=str) if args.json else render_generic_md("Strategy Readiness", report), end="")
        return 0
    except Exception as exc:
        print(f"strategy-readiness failed: {type(exc).__name__}: {exc}")
        return 2


def reconcile_live_order_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile one live broker order into canonical lifecycle diagnostics.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--user", default="live_bot")
    parser.add_argument("--order-id")
    parser.add_argument("--all-submitted", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        from src.config_loader import load_config
        from src.user_manager import UserManager

        base = load_config(PROJECT_ROOT / "config" / "default.yaml")
        mgr = UserManager(base, users_path=PROJECT_ROOT / "config" / "users.yaml", selected_user_id=args.user)
        broker = mgr.get_broker(args.user)
        if args.all_submitted:
            report = reconcile_submitted_broker_orders(
                root=PROJECT_ROOT,
                day=args.date,
                user=args.user,
                broker=broker,
                persist=not args.dry_run,
            )
        else:
            if not args.order_id:
                parser.error("--order-id is required unless --all-submitted is used")
            report = reconcile_broker_order_lifecycle(
                root=PROJECT_ROOT,
                day=args.date,
                user=args.user,
                broker=broker,
                order_id=args.order_id,
                symbol=args.symbol,
                persist=not args.dry_run,
            )
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True, default=str))
        else:
            print(render_generic_md("Live Order Reconciliation", report), end="")
        ok = bool(report.get("reconciled")) or (args.all_submitted and int(report.get("unresolved_count") or 0) == 0)
        return 0 if ok else 1
    except Exception as exc:
        print(f"reconcile-live-order failed: {type(exc).__name__}: {exc}")
        return 2


def experiment_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    report_p = sub.add_parser("report")
    report_p.add_argument("experiment_id")
    args = parser.parse_args(argv)
    report = experiment_list_report() if args.cmd == "list" else experiment_detail_report(args.experiment_id)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0
