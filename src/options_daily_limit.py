"""Audit and count live options daily-entry usage."""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.options_premium_risk import is_option_symbol

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
EXCLUDED_ORDER_STATUSES = {"rejected", "cancelled", "canceled", "expired", "new", "pending_new", "accepted", "submitted"}


@dataclass(frozen=True)
class OptionDailyLimitRecord:
    timestamp: str
    trading_date_et: str
    symbol: str
    asset_class: str
    option_contract_id: str
    strategy: str
    side: str
    order_status: str
    fill_status: str
    source: str
    counts: bool
    reason: str
    dedupe_key: str


@dataclass(frozen=True)
class OptionDailyLimitUsage:
    user_id: str
    environment: str
    trading_date_et: str
    timezone: str
    limit: int
    counted: int
    excluded: int
    records: tuple[OptionDailyLimitRecord, ...]

    @property
    def counted_records(self) -> tuple[OptionDailyLimitRecord, ...]:
        return tuple(row for row in self.records if row.counts)

    @property
    def excluded_records(self) -> tuple[OptionDailyLimitRecord, ...]:
        return tuple(row for row in self.records if not row.counts)


def options_state_path(root: str | Path, user_id: str) -> Path:
    base = Path(root)
    direct = base / f"options_positions_{user_id}.json"
    if direct.exists():
        return direct
    return base / "data" / f"options_positions_{user_id}.json"


def parse_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def et_trading_date(value: Any) -> str | None:
    dt = parse_timestamp(value)
    if dt is None:
        return None
    return dt.astimezone(ET).date().isoformat()


def today_et(now: datetime | None = None) -> str:
    dt = now or datetime.now(tz=ET)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).date().isoformat()


def _load_state(root: str | Path, user_id: str) -> Mapping[str, Any]:
    path = options_state_path(root, user_id)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def _strategy_from_reason(reason: Any) -> str:
    text = str(reason or "")
    for part in text.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key.strip() == "source":
            return value.strip() or "unknown"
    return "unknown"


def _fill_status(row: Mapping[str, Any]) -> str:
    status = str(row.get("entry_order_status") or row.get("order_status") or "").strip().lower()
    if status in {"rejected", "cancelled", "canceled", "expired"}:
        return "not_filled"
    if row.get("entry_fill_price") is not None:
        return "filled"
    if row.get("filled_at") or row.get("fill_id"):
        return "filled"
    return "not_filled"


def _dedupe_key(row: Mapping[str, Any], symbol: str, timestamp: str) -> str:
    for key in ("entry_fill_id", "fill_id", "entry_order_id", "order_id"):
        raw = row.get(key)
        if raw is not None and str(raw).strip():
            return f"{key}:{str(raw).strip()}"
    return "position:%s:%s" % (symbol, timestamp)


def _record_from_row(
    row: Mapping[str, Any],
    *,
    source: str,
    trading_date: str,
    environment: str,
    seen: set[str],
) -> OptionDailyLimitRecord:
    timestamp = str(row.get("entry_time") or row.get("timestamp") or row.get("created_at") or "")
    row_date = et_trading_date(timestamp) or "unknown"
    symbol = str(row.get("symbol") or "").strip().upper()
    asset_class = "option" if is_option_symbol(symbol) else "equity" if symbol else "unknown"
    side = str(row.get("entry_side") or row.get("side") or "buy").strip().lower()
    order_status = str(row.get("entry_order_status") or row.get("order_status") or "unknown").strip().lower()
    fill_status = _fill_status(row)
    strategy = _strategy_from_reason(row.get("entry_reason"))
    dedupe_key = _dedupe_key(row, symbol, timestamp)
    counts = True
    reason = "counted"
    if row_date != trading_date:
        counts, reason = False, "different_trading_date"
    elif environment == "live" and str(row.get("environment") or "live").strip().lower() == "paper":
        counts, reason = False, "paper_record_in_live_counter"
    elif asset_class != "option":
        counts, reason = False, "not_option_entry"
    elif side != "buy":
        counts, reason = False, "not_entry_buy"
    elif order_status in EXCLUDED_ORDER_STATUSES and fill_status != "filled":
        counts, reason = False, "not_successfully_filled"
    elif fill_status != "filled":
        counts, reason = False, "not_successfully_filled"
    elif dedupe_key in seen:
        counts, reason = False, "duplicate_entry"
    if counts:
        seen.add(dedupe_key)
    return OptionDailyLimitRecord(
        timestamp=timestamp or "unknown",
        trading_date_et=row_date,
        symbol=symbol,
        asset_class=asset_class,
        option_contract_id=symbol if asset_class == "option" else "",
        strategy=strategy,
        side=side,
        order_status=order_status,
        fill_status=fill_status,
        source=source,
        counts=counts,
        reason=reason,
        dedupe_key=dedupe_key,
    )


def _state_entry_rows(state: Mapping[str, Any]) -> list[tuple[Mapping[str, Any], str]]:
    out: list[tuple[Mapping[str, Any], str]] = []
    positions = state.get("positions")
    if isinstance(positions, Mapping):
        for key, row in positions.items():
            if isinstance(row, Mapping):
                merged = dict(row)
                merged.setdefault("symbol", key)
                out.append((merged, "options_positions.positions"))
    history = state.get("history")
    if isinstance(history, Sequence) and not isinstance(history, (str, bytes)):
        for idx, row in enumerate(history):
            if isinstance(row, Mapping):
                out.append((row, f"options_positions.history[{idx}]"))
    return out


def build_options_daily_limit_usage(
    *,
    root: str | Path,
    user_id: str,
    environment: str,
    limit: int,
    trading_date: str | date | None = None,
    now: datetime | None = None,
) -> OptionDailyLimitUsage:
    date_text = trading_date.isoformat() if isinstance(trading_date, date) else str(trading_date or today_et(now))
    path = options_state_path(root, user_id)
    state = _load_state(root, user_id)
    seen: set[str] = set()
    records = tuple(
        _record_from_row(
            row,
            source=f"{path.name}:{source}",
            trading_date=date_text,
            environment=environment,
            seen=seen,
        )
        for row, source in _state_entry_rows(state)
    )
    usage = OptionDailyLimitUsage(
        user_id=user_id,
        environment=environment,
        trading_date_et=date_text,
        timezone="America/New_York",
        limit=int(limit),
        counted=sum(1 for row in records if row.counts),
        excluded=sum(1 for row in records if not row.counts),
        records=records,
    )
    log.info(
        "OPTIONS_DAILY_LIMIT_USAGE limit=%d counted=%d excluded=%d date=%s timezone=America/New_York",
        usage.limit,
        usage.counted,
        usage.excluded,
        usage.trading_date_et,
    )
    return usage


def format_options_daily_limit_usage(usage: OptionDailyLimitUsage) -> list[str]:
    lines = [
        "OPTIONS_DAILY_LIMIT_USAGE limit=%d counted=%d excluded=%d date=%s timezone=America/New_York"
        % (usage.limit, usage.counted, usage.excluded, usage.trading_date_et),
        "OPTIONS_DAILY_LIMIT_RECORDS",
    ]
    for row in usage.records:
        lines.append(
            "record timestamp=%s trading_date=%s symbol=%s asset_class=%s option_contract=%s "
            "strategy=%s side=%s order_status=%s fill_status=%s source=%s counts=%s reason=%s dedupe_key=%s"
            % (
                row.timestamp,
                row.trading_date_et,
                row.symbol or "n/a",
                row.asset_class,
                row.option_contract_id or "n/a",
                row.strategy,
                row.side,
                row.order_status,
                row.fill_status,
                row.source,
                str(row.counts).lower(),
                row.reason,
                row.dedupe_key,
            )
        )
    return lines
