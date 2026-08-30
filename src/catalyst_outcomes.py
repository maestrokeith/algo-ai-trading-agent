"""Historical catalyst outcome extraction and aggregation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CatalystOutcome:
    """Observed return after a catalyst-tagged trade."""

    symbol: str
    catalyst_type: str
    news_score: float
    subsequent_return_pct: float
    observed_date: str
    catalyst_score: float = 0.0
    entry_price: float | None = None
    exit_price: float | None = None
    hold_duration_minutes: float | None = None
    source: str = "daily_trade"
    trade_id: str = ""

    @property
    def realized_return_pct(self) -> float:
        """Alias used by the JSON analytics store."""
        return self.subsequent_return_pct


@dataclass(frozen=True)
class HistoricalCatalystOutcome:
    """Research-only catalyst candidate and later outcome row."""

    date: str
    symbol: str
    source: str
    news_score: float | None = None
    event_score: float | None = None
    catalyst_score: float | None = None
    rank: int | None = None
    premarket_price: float | None = None
    open_price: float | None = None
    close_price: float | None = None
    one_day_return: float | None = None
    three_day_return: float | None = None
    five_day_return: float | None = None
    ten_day_return: float | None = None
    bot_bought: bool = False
    entry_price: float | None = None
    exit_price: float | None = None
    realized_pnl: float | None = None
    rejection_reason: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Return the stable JSON schema required by the research database."""
        return {
            "date": self.date,
            "symbol": self.symbol,
            "source": self.source,
            "news_score": self.news_score,
            "event_score": self.event_score,
            "catalyst_score": self.catalyst_score,
            "rank": self.rank,
            "premarket_price": self.premarket_price,
            "open_price": self.open_price,
            "close_price": self.close_price,
            "1d_return": self.one_day_return,
            "3d_return": self.three_day_return,
            "5d_return": self.five_day_return,
            "10d_return": self.ten_day_return,
            "bot_bought": self.bot_bought,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "realized_pnl": self.realized_pnl,
            "rejection_reason": self.rejection_reason,
        }


def _float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _int_or_none(value: Any) -> int | None:
    out = _float_or_none(value)
    return int(out) if out is not None else None


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _iter_rows(payload: Any, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _trade_return_pct(trade: Mapping[str, Any]) -> float | None:
    for key in ("return_pct", "pnl_pct", "realized_return_pct", "profit_loss_pct"):
        value = _float_or_none(trade.get(key))
        if value is not None:
            return value
    pnl = _float_or_none(trade.get("pnl"))
    qty = _float_or_none(trade.get("qty"))
    price = _float_or_none(trade.get("filled_avg_price"))
    if pnl is None or qty is None or price is None:
        return None
    notional = abs(qty * price)
    if notional <= 0:
        return None
    return (pnl / notional) * 100.0


def _first_float(trade: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = _float_or_none(trade.get(key))
        if value is not None:
            return value
    return None


def _entry_exit_prices(trade: Mapping[str, Any]) -> tuple[float | None, float | None]:
    entry = _first_float(trade, ("entry_price", "avg_entry_price", "buy_price", "filled_avg_price"))
    exit_price = _first_float(trade, ("exit_price", "avg_exit_price", "sell_price"))
    if exit_price is None:
        exit_price = _first_float(trade, ("filled_exit_price", "close_price"))
    return entry, exit_price


def _hold_duration_minutes(trade: Mapping[str, Any]) -> float | None:
    direct = _first_float(trade, ("hold_duration_minutes", "hold_minutes", "duration_minutes"))
    if direct is not None:
        return direct
    hours = _first_float(trade, ("hold_hours", "duration_hours"))
    return hours * 60.0 if hours is not None else None


def outcome_from_trade(
    trade: Mapping[str, Any],
    *,
    observed_date: date | str,
) -> CatalystOutcome | None:
    """Build a catalyst outcome from one normalized trade row, when metadata exists."""
    symbol = str(trade.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    catalyst_type = str(trade.get("catalyst_type") or trade.get("source") or "").strip().lower()
    news_score = _float_or_none(trade.get("news_score"))
    catalyst_score = _float_or_none(trade.get("catalyst_score")) or 0.0
    if not catalyst_type and news_score is None:
        return None
    if not catalyst_type:
        catalyst_type = "news"
    ret = _trade_return_pct(trade)
    if ret is None:
        return None
    entry_price, exit_price = _entry_exit_prices(trade)
    return CatalystOutcome(
        symbol=symbol,
        catalyst_type=catalyst_type,
        news_score=float(news_score or 0.0),
        subsequent_return_pct=float(ret),
        observed_date=str(observed_date),
        catalyst_score=float(catalyst_score),
        entry_price=entry_price,
        exit_price=exit_price,
        hold_duration_minutes=_hold_duration_minutes(trade),
        source=str(trade.get("strategy") or trade.get("source") or "daily_trade"),
        trade_id=str(trade.get("id") or trade.get("order_id") or ""),
    )


def outcomes_from_trades(
    trades: Sequence[Mapping[str, Any]],
    *,
    observed_date: date | str,
) -> list[CatalystOutcome]:
    """Extract all catalyst outcomes from normalized daily trade rows."""
    outcomes: list[CatalystOutcome] = []
    for trade in trades:
        out = outcome_from_trade(trade, observed_date=observed_date)
        if out is not None:
            outcomes.append(out)
    return outcomes


def record_catalyst_outcomes_from_trades(
    store: Any,
    *,
    user_id: str | None,
    trades: Sequence[Mapping[str, Any]],
    observed_date: date | str,
) -> int:
    """Persist catalyst outcomes to a store exposing ``record_catalyst_outcome``."""
    recorder = getattr(store, "record_catalyst_outcome", None)
    if not callable(recorder):
        return 0
    count = 0
    for outcome in outcomes_from_trades(trades, observed_date=observed_date):
        recorder(
            user_id=user_id,
            symbol=outcome.symbol,
            catalyst_type=outcome.catalyst_type,
            news_score=outcome.news_score,
            subsequent_return_pct=outcome.subsequent_return_pct,
            observed_date=outcome.observed_date,
            source=outcome.source,
            trade_id=outcome.trade_id,
        )
        count += 1
    return count


def outcome_to_record(outcome: CatalystOutcome, *, user_id: str | None = None) -> dict[str, Any]:
    """Serialize a catalyst outcome for ``data/analytics/catalyst_outcomes.json``."""
    return {
        "user_id": user_id,
        "symbol": outcome.symbol,
        "date": outcome.observed_date,
        "catalyst_type": outcome.catalyst_type,
        "catalyst_score": outcome.catalyst_score,
        "news_score": outcome.news_score,
        "entry_price": outcome.entry_price,
        "exit_price": outcome.exit_price,
        "realized_return_pct": outcome.realized_return_pct,
        "hold_duration_minutes": outcome.hold_duration_minutes,
        "source": outcome.source,
        "trade_id": outcome.trade_id,
    }


def load_catalyst_outcome_records(path: str | Path = "data/analytics/catalyst_outcomes.json") -> list[dict[str, Any]]:
    """Load catalyst outcome records from the JSON analytics store."""
    store_path = Path(path)
    if not store_path.exists():
        return []
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        rows = payload.get("outcomes") or []
    else:
        rows = payload
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def save_catalyst_outcome_records(
    records: Sequence[Mapping[str, Any]],
    path: str | Path = "data/analytics/catalyst_outcomes.json",
) -> Path:
    """Write catalyst outcome records to the JSON analytics store."""
    store_path = Path(path)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"outcomes": [dict(row) for row in records]}
    store_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return store_path


def append_catalyst_outcomes_json(
    trades: Sequence[Mapping[str, Any]],
    *,
    observed_date: date | str,
    user_id: str | None = None,
    path: str | Path = "data/analytics/catalyst_outcomes.json",
) -> int:
    """Append completed trade outcomes to the JSON analytics store."""
    existing = load_catalyst_outcome_records(path)
    seen = {
        (
            str(row.get("user_id") or ""),
            str(row.get("trade_id") or ""),
            str(row.get("symbol") or ""),
            str(row.get("date") or ""),
        )
        for row in existing
    }
    added = 0
    for outcome in outcomes_from_trades(trades, observed_date=observed_date):
        record = outcome_to_record(outcome, user_id=user_id)
        key = (
            str(record.get("user_id") or ""),
            str(record.get("trade_id") or ""),
            str(record.get("symbol") or ""),
            str(record.get("date") or ""),
        )
        if key in seen:
            continue
        existing.append(record)
        seen.add(key)
        added += 1
    save_catalyst_outcome_records(existing, path)
    return added


def summarize_catalyst_outcomes(
    outcomes: Sequence[CatalystOutcome | Mapping[str, Any]],
) -> dict[str, dict[str, float]]:
    """Aggregate rolling outcome statistics by catalyst type."""
    buckets: dict[str, list[float]] = {}
    for row in outcomes:
        if isinstance(row, CatalystOutcome):
            catalyst_type = row.catalyst_type
            ret = row.subsequent_return_pct
        else:
            catalyst_type = str(row.get("catalyst_type") or "unknown")
            value = _float_or_none(row.get("subsequent_return_pct"))
            if value is None:
                value = _float_or_none(row.get("realized_return_pct"))
            if value is None:
                continue
            ret = value
        buckets.setdefault(str(catalyst_type or "unknown").lower(), []).append(float(ret))
    summary: dict[str, dict[str, float]] = {}
    for catalyst_type, returns in buckets.items():
        wins = sum(1 for value in returns if value > 0)
        gross_profit = sum(value for value in returns if value > 0)
        gross_loss = abs(sum(value for value in returns if value < 0))
        summary[catalyst_type] = {
            "sample_count": float(len(returns)),
            "count": float(len(returns)),
            "win_rate_pct": (wins / len(returns) * 100.0) if returns else 0.0,
            "avg_return_pct": (sum(returns) / len(returns)) if returns else 0.0,
            "median_return_pct": float(median(returns)) if returns else 0.0,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (0.0 if gross_profit <= 0 else float("inf")),
        }
    return summary


def historical_catalyst_outcome_path(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> Path:
    """Return the research-only historical catalyst outcome database path."""
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    user_s = str(user_id or "default").strip() or "default"
    return Path(data_dir) / "research" / "catalyst_outcomes" / f"{day_s}_{user_s}.json"


def historical_catalyst_summary_path(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> Path:
    """Return the companion plain-text summary path."""
    return historical_catalyst_outcome_path(data_dir=data_dir, user_id=user_id, day=day).with_suffix(".txt")


def _compact_date_from_name(path: Path) -> str | None:
    stem = path.name[:8]
    if len(stem) == 8 and stem.isdigit():
        return f"{stem[:4]}-{stem[4:6]}-{stem[6:8]}"
    return None


def _iso_date_from_payload(payload: Mapping[str, Any]) -> str | None:
    raw = payload.get("date") or payload.get("generated_at")
    if isinstance(raw, str) and len(raw) >= 10:
        text = raw[:10]
        if text[4:5] == "-" and text[7:8] == "-":
            return text
    return None


def latest_historical_catalyst_date(*, data_dir: Path | str, user_id: str = "default") -> str | None:
    """Return the latest date with local catalyst candidate or outcome artifacts."""
    data = Path(data_dir)
    user_s = str(user_id or "default").strip() or "default"
    candidates: set[str] = set()
    outcome_dir = data / "research" / "catalyst_outcomes"
    if outcome_dir.exists():
        for path in outcome_dir.glob(f"*_{user_s}.json"):
            day = path.name.split("_", 1)[0]
            if len(day) == 10:
                candidates.add(day)
    scan_dir = data / "dynamic_scan_history"
    if scan_dir.exists():
        for path in scan_dir.glob(f"*_{user_s}.json"):
            day = _compact_date_from_name(path)
            if day:
                candidates.add(day)
    attr_dir = data / "trade_attribution" / "daily"
    if attr_dir.exists():
        for path in attr_dir.glob(f"*_{user_s}.json"):
            day = path.name.split("_", 1)[0]
            if len(day) == 10:
                candidates.add(day)
    for path in (data / "premarket" / "latest_rankings.json", data / "premarket" / "latest_catalysts.json"):
        payload = _load_json(path)
        if isinstance(payload, Mapping):
            day = _iso_date_from_payload(payload)
            if day:
                candidates.add(day)
    return max(candidates) if candidates else None


def _candidate_score(row: Mapping[str, Any]) -> float:
    values = (
        _float_or_none(row.get("catalyst_score")),
        _float_or_none(row.get("event_score")),
        _float_or_none(row.get("news_score")),
        _float_or_none(row.get("score")),
    )
    return max((float(value) for value in values if value is not None), default=0.0)


def _candidate_from_row(
    row: Mapping[str, Any],
    *,
    day: str,
    source: str,
    rank: int | None = None,
) -> dict[str, Any] | None:
    symbol = str(row.get("symbol") or row.get("sym_u") or "").strip().upper()
    if not symbol:
        return None
    candidate_source = str(row.get("source") or row.get("route") or source or "candidate").strip() or "candidate"
    rejection = _text_or_none(row.get("rejection_reason") or row.get("reason") or row.get("no_action_reason"))
    return {
        "date": day,
        "symbol": symbol,
        "source": candidate_source,
        "news_score": _float_or_none(row.get("news_score")),
        "event_score": _float_or_none(row.get("event_score")),
        "catalyst_score": _float_or_none(row.get("catalyst_score") if row.get("catalyst_score") is not None else row.get("score")),
        "rank": rank if rank is not None else _int_or_none(row.get("rank") or row.get("selected_rank")),
        "premarket_price": _float_or_none(
            row.get("premarket_price") if row.get("premarket_price") is not None else row.get("price")
        ),
        "rejection_reason": rejection,
        "_score": _candidate_score(row),
        "_accepted": bool(row.get("accepted")) if row.get("accepted") is not None else rejection is None,
    }


def _merge_candidate(existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    if existing is None:
        return dict(incoming)
    if bool(incoming.get("_accepted")) and not bool(existing.get("_accepted")):
        preferred, other = dict(incoming), existing
    elif float(incoming.get("_score") or 0.0) > float(existing.get("_score") or 0.0):
        preferred, other = dict(incoming), existing
    else:
        preferred, other = dict(existing), incoming
    for key in (
        "news_score",
        "event_score",
        "catalyst_score",
        "rank",
        "premarket_price",
        "rejection_reason",
    ):
        if preferred.get(key) is None and other.get(key) is not None:
            preferred[key] = other.get(key)
    if other.get("source") and other.get("source") not in str(preferred.get("source") or ""):
        preferred["source"] = f"{preferred.get('source')},{other.get('source')}"
    return preferred


def _load_dynamic_scan_candidates(*, data_dir: Path, user_id: str, day: str) -> list[dict[str, Any]]:
    scan_dir = data_dir / "dynamic_scan_history"
    if not scan_dir.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(scan_dir.glob(f"{day.replace('-', '')}T*_{user_id}.json")):
        payload = _load_json(path)
        if not isinstance(payload, Mapping):
            continue
        for idx, row in enumerate(_iter_rows(payload, ("candidates", "accepted", "rejected")), start=1):
            candidate = _candidate_from_row(row, day=day, source="dynamic_scan", rank=idx)
            if candidate is not None:
                rows.append(candidate)
    return rows


def _load_premarket_candidates(*, data_dir: Path, day: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path, keys, source in (
        (data_dir / "premarket" / "latest_rankings.json", ("items", "rankings"), "premarket_rankings"),
        (data_dir / "premarket" / "latest_catalysts.json", ("catalysts",), "premarket_catalysts"),
    ):
        payload = _load_json(path)
        if not isinstance(payload, Mapping) or _iso_date_from_payload(payload) != day:
            continue
        for idx, row in enumerate(_iter_rows(payload, keys), start=1):
            candidate = _candidate_from_row(row, day=day, source=source, rank=idx)
            if candidate is not None:
                out.append(candidate)
    return out


def _load_attribution_candidates(*, data_dir: Path, user_id: str, day: str) -> list[dict[str, Any]]:
    path = data_dir / "trade_attribution" / "daily" / f"{day}_{user_id}.json"
    payload = _load_json(path)
    if not isinstance(payload, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for key in ("candidates", "allocator_candidates"):
        for idx, row in enumerate(_iter_rows(payload, (key,)), start=1):
            dynamic = bool(row.get("dynamic"))
            has_catalyst = any(_float_or_none(row.get(k)) not in (None, 0.0) for k in ("news_score", "event_score", "catalyst_score"))
            source = str(row.get("source") or row.get("route") or "").lower()
            if not (dynamic or has_catalyst or "news" in source or "catalyst" in source):
                continue
            candidate = _candidate_from_row(row, day=day, source=key, rank=idx)
            if candidate is not None:
                rows.append(candidate)
    return rows


def collect_historical_catalyst_candidates(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> list[dict[str, Any]]:
    """Collect daily dynamic/news/catalyst candidates from local artifacts."""
    data = Path(data_dir)
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    user_s = str(user_id or "default").strip() or "default"
    by_symbol: dict[str, dict[str, Any]] = {}
    for row in (
        _load_dynamic_scan_candidates(data_dir=data, user_id=user_s, day=day_s)
        + _load_attribution_candidates(data_dir=data, user_id=user_s, day=day_s)
        + _load_premarket_candidates(data_dir=data, day=day_s)
    ):
        symbol = str(row.get("symbol") or "")
        by_symbol[symbol] = _merge_candidate(by_symbol.get(symbol), row)
    candidates = sorted(
        by_symbol.values(),
        key=lambda row: (row.get("rank") is None, int(row.get("rank") or 999999), -float(row.get("_score") or 0.0), row.get("symbol") or ""),
    )
    return [{k: v for k, v in row.items() if not k.startswith("_")} for row in candidates]


def _daily_price_rows(data_dir: Path, symbol: str) -> list[dict[str, Any]]:
    bars_dir = data_dir / "historical_bars"
    candidates = (
        bars_dir / f"{symbol}.json",
        bars_dir / f"{symbol}_1Day.json",
        bars_dir / f"{symbol}.csv",
        bars_dir / f"{symbol}_1Day.csv",
    )
    for path in candidates:
        if not path.exists():
            continue
        if path.suffix == ".json":
            payload = _load_json(path)
            rows = _iter_rows(payload, ("bars", "data", "rows"))
            return [dict(row) for row in rows]
        rows: list[dict[str, Any]] = []
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return rows
        headers = [item.strip() for item in lines[0].split(",")]
        for line in lines[1:]:
            if not line.strip():
                continue
            values = [item.strip() for item in line.split(",")]
            rows.append(dict(zip(headers, values)))
        return rows
    return []


def _row_date(row: Mapping[str, Any]) -> str | None:
    raw = row.get("date") or row.get("timestamp") or row.get("t")
    if isinstance(raw, str) and len(raw) >= 10:
        return raw[:10]
    return None


def _price_outcomes(*, data_dir: Path, symbol: str, day: str) -> dict[str, float | None]:
    rows = sorted(_daily_price_rows(data_dir, symbol), key=lambda row: _row_date(row) or "")
    idx = next((i for i, row in enumerate(rows) if _row_date(row) == day), None)
    if idx is None:
        return {
            "open_price": None,
            "close_price": None,
            "1d_return": None,
            "3d_return": None,
            "5d_return": None,
            "10d_return": None,
        }
    start = _float_or_none(rows[idx].get("open") if rows[idx].get("open") is not None else rows[idx].get("o"))
    close = _float_or_none(rows[idx].get("close") if rows[idx].get("close") is not None else rows[idx].get("c"))

    def _ret(offset: int) -> float | None:
        if start is None or start <= 0 or idx + offset >= len(rows):
            return None
        end = _float_or_none(
            rows[idx + offset].get("close")
            if rows[idx + offset].get("close") is not None
            else rows[idx + offset].get("c")
        )
        return round(((end / start) - 1.0) * 100.0, 6) if end is not None else None

    return {
        "open_price": start,
        "close_price": close,
        "1d_return": _ret(0),
        "3d_return": _ret(2),
        "5d_return": _ret(4),
        "10d_return": _ret(9),
    }


def _trade_outcomes(*, data_dir: Path, user_id: str, day: str) -> dict[str, dict[str, Any]]:
    path = data_dir / "trade_attribution" / "daily" / f"{day}_{user_id}.json"
    payload = _load_json(path)
    if not isinstance(payload, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in _iter_rows(payload, ("orders",)):
        side = str(row.get("side") or row.get("action") or "").lower()
        if side and "buy" not in side:
            continue
        if row.get("submitted") is False or row.get("action_created") is False:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        entry = _first_float(row, ("entry_price", "filled_avg_price", "avg_entry_price", "limit_price", "price"))
        cur = out.setdefault(symbol, {"bot_bought": True})
        cur["bot_bought"] = True
        if entry is not None and cur.get("entry_price") is None:
            cur["entry_price"] = entry
    for row in _iter_rows(payload, ("exits",)):
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        cur = out.setdefault(symbol, {})
        cur["bot_bought"] = bool(cur.get("bot_bought", True))
        for target, keys in (
            ("entry_price", ("entry_price", "avg_entry_price", "buy_price")),
            ("exit_price", ("exit_price", "avg_exit_price", "sell_price", "filled_exit_price", "close_price")),
            ("realized_pnl", ("realized_pnl", "pnl", "profit_loss", "realized_profit_loss")),
        ):
            value = _first_float(row, keys)
            if value is not None:
                cur[target] = value
    return out


def build_historical_catalyst_outcomes(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> list[dict[str, Any]]:
    """Build research-only historical catalyst outcome rows from local artifacts."""
    data = Path(data_dir)
    day_s = day.isoformat() if isinstance(day, date) else str(day)
    trade_by_symbol = _trade_outcomes(data_dir=data, user_id=str(user_id or "default"), day=day_s)
    rows: list[dict[str, Any]] = []
    for candidate in collect_historical_catalyst_candidates(data_dir=data, user_id=user_id, day=day_s):
        symbol = str(candidate["symbol"])
        prices = _price_outcomes(data_dir=data, symbol=symbol, day=day_s)
        trade = trade_by_symbol.get(symbol, {})
        bought = bool(trade.get("bot_bought"))
        rejection = candidate.get("rejection_reason")
        if not bought and not rejection:
            rejection = "not_bought"
        row = HistoricalCatalystOutcome(
            date=day_s,
            symbol=symbol,
            source=str(candidate.get("source") or "candidate"),
            news_score=_float_or_none(candidate.get("news_score")),
            event_score=_float_or_none(candidate.get("event_score")),
            catalyst_score=_float_or_none(candidate.get("catalyst_score")),
            rank=_int_or_none(candidate.get("rank")),
            premarket_price=_float_or_none(candidate.get("premarket_price")),
            open_price=prices["open_price"],
            close_price=prices["close_price"],
            one_day_return=prices["1d_return"],
            three_day_return=prices["3d_return"],
            five_day_return=prices["5d_return"],
            ten_day_return=prices["10d_return"],
            bot_bought=bought,
            entry_price=_float_or_none(trade.get("entry_price")),
            exit_price=_float_or_none(trade.get("exit_price")),
            realized_pnl=_float_or_none(trade.get("realized_pnl")),
            rejection_reason=str(rejection) if rejection else None,
        ).to_record()
        rows.append(row)
    return rows


def save_historical_catalyst_outcomes(
    rows: Sequence[Mapping[str, Any]],
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> Path:
    """Persist the research-only catalyst outcome database."""
    path = historical_catalyst_outcome_path(data_dir=data_dir, user_id=user_id, day=day)
    payload = {
        "version": 1,
        "date": day.isoformat() if isinstance(day, date) else str(day),
        "user_id": str(user_id or "default"),
        "generated_at": datetime.now().isoformat(),
        "records": [dict(row) for row in rows],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _outcome_return(row: Mapping[str, Any]) -> float | None:
    for key in ("10d_return", "5d_return", "3d_return", "1d_return"):
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _score_bucket(value: Any) -> str:
    score = _float_or_none(value)
    if score is None:
        return "missing"
    normalized = score / 10.0 if score > 1.0 else score
    if normalized < 0.3:
        return "<0.30"
    if normalized < 0.6:
        return "0.30-0.59"
    if normalized < 0.8:
        return "0.60-0.79"
    return ">=0.80"


def summarize_historical_catalyst_outcomes(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize historical catalyst candidates for the CLI."""
    records = [dict(row) for row in rows]
    bought = [row for row in records if bool(row.get("bot_bought"))]
    missed = [row for row in records if not bool(row.get("bot_bought"))]
    with_returns = [row for row in records if _outcome_return(row) is not None]
    buckets: dict[str, list[float]] = {}
    for row in with_returns:
        ret = _outcome_return(row)
        if ret is not None:
            buckets.setdefault(_score_bucket(row.get("catalyst_score")), []).append(ret)
    return {
        "candidate_count": len(records),
        "bought_count": len(bought),
        "missed_count": len(missed),
        "top_catalysts": sorted(records, key=lambda row: _float_or_none(row.get("catalyst_score")) or -1.0, reverse=True)[:5],
        "best_missed_winners": sorted(
            (row for row in missed if _outcome_return(row) is not None),
            key=lambda row: _outcome_return(row) or 0.0,
            reverse=True,
        )[:5],
        "worst_bought_losers": sorted(
            (row for row in bought if _outcome_return(row) is not None),
            key=lambda row: _outcome_return(row) or 0.0,
        )[:5],
        "avg_return_by_catalyst_score_bucket": {
            bucket: {
                "count": len(values),
                "avg_return": sum(values) / len(values) if values else 0.0,
            }
            for bucket, values in sorted(buckets.items())
        },
    }


def format_historical_catalyst_summary(
    *,
    day: date | str,
    user_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Render a concise research-only summary for the CLI."""
    summary = summarize_historical_catalyst_outcomes(rows)

    def _fmt_ret(row: Mapping[str, Any]) -> str:
        ret = _outcome_return(row)
        return "n/a" if ret is None else f"{ret:.2f}%"

    def _fmt_score(value: Any) -> str:
        score = _float_or_none(value)
        return "n/a" if score is None else f"{score:.2f}"

    lines = [
        f"Historical Catalyst Outcomes {day} [{user_id}]",
        (
            "Bought vs missed: "
            f"candidates={summary['candidate_count']} "
            f"bought={summary['bought_count']} missed={summary['missed_count']}"
        ),
        "Top catalysts:",
    ]
    for row in summary["top_catalysts"]:
        lines.append(
            f"  {row.get('symbol')} score={_fmt_score(row.get('catalyst_score'))} "
            f"source={row.get('source')} bought={bool(row.get('bot_bought'))} return={_fmt_ret(row)}"
        )
    lines.append("Best missed winners:")
    for row in summary["best_missed_winners"]:
        lines.append(
            f"  {row.get('symbol')} return={_fmt_ret(row)} score={_fmt_score(row.get('catalyst_score'))} "
            f"reason={row.get('rejection_reason') or 'not_bought'}"
        )
    lines.append("Worst bought losers:")
    for row in summary["worst_bought_losers"]:
        lines.append(
            f"  {row.get('symbol')} return={_fmt_ret(row)} score={_fmt_score(row.get('catalyst_score'))} "
            f"pnl={row.get('realized_pnl') if row.get('realized_pnl') is not None else 'n/a'}"
        )
    lines.append("Average return by catalyst_score bucket:")
    buckets = summary["avg_return_by_catalyst_score_bucket"]
    if not buckets:
        lines.append("  no return data")
    for bucket, stats in buckets.items():
        lines.append(f"  {bucket}: n={stats['count']} avg={stats['avg_return']:.2f}%")
    return "\n".join(lines)


def write_historical_catalyst_outcome_report(
    *,
    data_dir: Path | str,
    user_id: str,
    day: date | str,
) -> tuple[Path, Path, str]:
    """Build, save, and render the research-only historical catalyst report."""
    rows = build_historical_catalyst_outcomes(data_dir=data_dir, user_id=user_id, day=day)
    json_path = save_historical_catalyst_outcomes(rows, data_dir=data_dir, user_id=user_id, day=day)
    text = format_historical_catalyst_summary(day=day, user_id=user_id, rows=rows)
    summary_path = historical_catalyst_summary_path(data_dir=data_dir, user_id=user_id, day=day)
    summary_path.write_text(text + "\n", encoding="utf-8")
    return json_path, summary_path, text


__all__ = [
    "CatalystOutcome",
    "HistoricalCatalystOutcome",
    "build_historical_catalyst_outcomes",
    "collect_historical_catalyst_candidates",
    "format_historical_catalyst_summary",
    "historical_catalyst_outcome_path",
    "historical_catalyst_summary_path",
    "latest_historical_catalyst_date",
    "outcome_from_trade",
    "outcomes_from_trades",
    "append_catalyst_outcomes_json",
    "load_catalyst_outcome_records",
    "outcome_to_record",
    "record_catalyst_outcomes_from_trades",
    "save_catalyst_outcome_records",
    "save_historical_catalyst_outcomes",
    "summarize_historical_catalyst_outcomes",
    "summarize_catalyst_outcomes",
    "write_historical_catalyst_outcome_report",
]
