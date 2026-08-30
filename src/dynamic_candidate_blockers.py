"""Research-only report for selected dynamic candidates blocked downstream."""

from __future__ import annotations

import gzip
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_SELECTED_RE = re.compile(r"\bDYNAMIC_SELECTED\s+symbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b")
_SCAN_RE = re.compile(r"\bDYNAMIC_SCAN\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<body>.+)$")
_SCAN_REJECT_RE = re.compile(r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<reason>.+)$")
_SKIP_RE = re.compile(r"\bSKIP\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+reason=(?P<reason>.+)$")
_BREAKOUT_TEXT = "need 5m breakout OR new intraday high OR strong green 1m OR opening-range breakout"
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


@dataclass(frozen=True)
class DynamicCandidateBlockerPaths:
    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _round(value: Any, ndigits: int = 4) -> float | None:
    number = _safe_float(value)
    return round(number, ndigits) if number is not None else None


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).rstrip(",;%") for match in _KV_RE.finditer(line)}


def _date_from_compact(raw: str) -> str | None:
    if len(raw) != 8 or not raw.isdigit():
        return None
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"


def _date_from_path(path: Path) -> str | None:
    iso = _ISO_DATE_RE.search(path.name)
    if iso:
        return iso.group(1)
    compact = _COMPACT_DATE_RE.search(path.name)
    if compact:
        return _date_from_compact(compact.group(1))
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ET)
    return dt.astimezone(_ET)


def _line_timestamp(line: str, *, day: str) -> datetime | None:
    iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)", line)
    if iso:
        return _parse_timestamp(iso.group(1))
    match = re.match(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\b", line)
    if not match:
        return None
    expected = datetime.strptime(day, "%Y-%m-%d").date()
    month = _MONTHS.get(match.group("mon"))
    if month != expected.month or int(match.group("day")) != expected.day:
        return None
    hh, mm, ss = (int(part) for part in match.group("time").split(":"))
    return datetime(expected.year, expected.month, expected.day, hh, mm, ss, tzinfo=_ET)


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _normalize_blocker(reason: str) -> str | None:
    text = reason.lower()
    if "not enough bars" in text and "need 200" in text:
        return "short_history_need_200"
    if "entry_alignment" in text and _BREAKOUT_TEXT.lower() in text:
        return "entry_alignment_breakout_new_high_orb"
    return None


def _bar_roots(data_dir: Path, bars_dir: Path | str | None) -> list[Path]:
    if bars_dir is not None:
        return [Path(bars_dir)]
    return [
        data_dir / "research" / "allocator_candidate_bars",
        data_dir / "research" / "dynamic_candidate_bars",
        data_dir / "historical_bars",
        data_dir / "bars",
        data_dir / "market",
        data_dir / "market_bars",
        data_dir / "intraday_bars",
        data_dir / "intraday_snapshots",
        data_dir / "snapshots",
        data_dir / "alpaca_cache",
        data_dir / "cache" / "alpaca",
        data_dir / "replay",
        data_dir / "replay_market_session",
        data_dir / "debug_logs",
    ]


def _bar_file_patterns(symbol: str, day: str) -> list[str]:
    compact = day.replace("-", "")
    patterns: list[str] = []
    for suffix in ("csv", "json"):
        patterns.extend(
            [
                f"**/{symbol}*{day}*.{suffix}",
                f"**/{day}*{symbol}*.{suffix}",
                f"**/{symbol}*{compact}*.{suffix}",
                f"**/{compact}*{symbol}*.{suffix}",
                f"**/{symbol}.{suffix}",
            ]
        )
    return patterns


def _frame_from_rows(rows: Any) -> pd.DataFrame | None:
    if not isinstance(rows, list):
        return None
    frame = pd.DataFrame([row for row in rows if isinstance(row, Mapping)])
    return frame if not frame.empty else None


def _extract_nested_bar_frames(payload: Any, *, symbol: str) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []

    def visit(obj: Any) -> None:
        if isinstance(obj, Mapping):
            raw_symbol = str(obj.get("symbol") or obj.get("Symbol") or "").strip().upper()
            for key in ("bars", "bars_1m", "ohlcv", "candles", "data"):
                value = obj.get(key)
                if raw_symbol == symbol and isinstance(value, list):
                    frame = _frame_from_rows(value)
                    if frame is not None:
                        frames.append(frame)
            for value in obj.values():
                visit(value)
            return
        if isinstance(obj, list):
            if obj and all(isinstance(row, Mapping) for row in obj):
                matching = [
                    row
                    for row in obj
                    if str(row.get("symbol") or row.get("Symbol") or "").strip().upper() == symbol
                ]
                if matching and any(
                    any(col in row for col in ("timestamp", "datetime", "time", "ts", "t", "date", "close", "c"))
                    for row in matching
                ):
                    frame = pd.DataFrame(matching)
                    if not frame.empty:
                        frames.append(frame)
            for value in obj:
                visit(value)

    visit(payload)
    return frames


def _read_bar_candidate(path: Path, *, symbol: str) -> tuple[pd.DataFrame | None, str]:
    try:
        if path.suffix.lower() == ".csv":
            return pd.read_csv(path), "csv"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            rows = payload.get("bars") or payload.get("bars_1m") or payload.get("ohlcv") or payload.get("candles")
            frame = _frame_from_rows(rows)
            if frame is not None:
                return frame, "json"
        elif isinstance(payload, list):
            frame = _frame_from_rows(payload)
            if frame is not None:
                return frame, "json"
        nested = _extract_nested_bar_frames(payload, symbol=symbol)
        if nested:
            return pd.concat(nested, ignore_index=True), "json_nested"
    except Exception:
        return None, path.suffix.lower().lstrip(".") or "unknown"
    return None, path.suffix.lower().lstrip(".") or "unknown"


def _bar_time_bounds(bars: pd.DataFrame | None, *, day: str) -> tuple[str | None, str | None]:
    if bars is None or bars.empty:
        return None, None
    timestamps = _bar_timestamps_utc(bars)
    if timestamps is None:
        return None, None
    day_ts = timestamps.loc[timestamps.dt.tz_convert(_ET).dt.date.astype(str) == day]
    if day_ts.empty:
        return None, None
    return day_ts.min().tz_convert(_ET).isoformat(), day_ts.max().tz_convert(_ET).isoformat()


def _load_local_bars_with_diagnostics(
    *,
    data_dir: Path,
    bars_dir: Path | str | None,
    symbol: str,
    day: str,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    sym = str(symbol or "").strip().upper()
    roots = _bar_roots(data_dir, bars_dir)
    patterns = _bar_file_patterns(sym, day) if sym else []
    diagnostics: dict[str, Any] = {
        "symbol": sym,
        "searched_roots": [str(root) for root in roots],
        "existing_roots": [str(root) for root in roots if root.exists()],
        "file_patterns": patterns,
        "candidate_files": [],
        "found_file": None,
        "found_format": None,
        "first_available_bar_time": None,
        "last_available_bar_time": None,
        "missing_bar_reason": None,
    }
    if not sym:
        diagnostics["missing_bar_reason"] = "missing_symbol"
        return None, diagnostics
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            candidates.extend(path for path in root.glob(pattern) if path.is_file())
    # Replay/debug files often encode the symbol inside date-named JSON rather than filename.
    for root in roots:
        if root.name not in {"replay", "replay_market_session", "debug_logs"} or not root.exists():
            continue
        compact = day.replace("-", "")
        for pattern in (f"**/*{day}*.json", f"**/*{compact}*.json"):
            candidates.extend(path for path in root.glob(pattern) if path.is_file())
    candidates = sorted(dict.fromkeys(candidates))
    diagnostics["candidate_files"] = [str(path) for path in candidates]
    for path in sorted(dict.fromkeys(candidates)):
        frame, fmt = _read_bar_candidate(path, symbol=sym)
        if isinstance(frame, pd.DataFrame) and not frame.empty:
            first_time, last_time = _bar_time_bounds(frame, day=day)
            if first_time is None or last_time is None:
                continue
            diagnostics["found_file"] = str(path)
            diagnostics["found_format"] = fmt
            diagnostics["first_available_bar_time"] = first_time
            diagnostics["last_available_bar_time"] = last_time
            return frame, diagnostics
    if not diagnostics["existing_roots"]:
        diagnostics["missing_bar_reason"] = "no_bar_roots_exist"
    elif not candidates:
        diagnostics["missing_bar_reason"] = "no_matching_bar_files"
    else:
        diagnostics["missing_bar_reason"] = "no_matching_bars_for_date"
    return None, diagnostics


def _bar_timestamps_utc(bars: pd.DataFrame) -> pd.Series | None:
    if bars.empty:
        return None
    if isinstance(bars.index, pd.DatetimeIndex):
        parsed = pd.Series(bars.index, index=bars.index)
    else:
        parsed = None
        for col in ("timestamp", "datetime", "time", "ts", "t", "date"):
            if col in bars.columns:
                parsed = pd.to_datetime(bars[col], utc=True, errors="coerce")
                break
        if parsed is None:
            return None
    parsed = pd.to_datetime(parsed, utc=True, errors="coerce")
    return parsed if parsed.notna().any() else None


def _pct_return(price: float | None, base_price: float) -> float | None:
    if price is None or base_price <= 0:
        return None
    return round(((float(price) / base_price) - 1.0) * 100.0, 4)


def _first_return_after_minutes(
    later: pd.DataFrame,
    *,
    close_col: str,
    rejected_utc: datetime,
    base_price: float,
    minutes: int,
) -> float | None:
    horizon = later.loc[later["_ts_utc"] >= rejected_utc + pd.Timedelta(minutes=minutes)]
    if horizon.empty:
        return None
    close = _safe_float(horizon.iloc[0].get(close_col))
    return _pct_return(close, base_price)


def _outcome_from_bars(
    bars: pd.DataFrame | None,
    *,
    rejected_at: datetime | None,
    rejected_price: float | None,
    day: str,
) -> dict[str, Any]:
    if rejected_at is None:
        return {
            "subsequent_intraday_return_pct": None,
            "max_gain_after_rejection_pct": None,
            "prevented_profitable_trade": None,
            "outcome_available": False,
            "missing_reason": "missing_rejection_timestamp",
        }
    if bars is None or bars.empty:
        return {
            "subsequent_intraday_return_pct": None,
            "max_gain_after_rejection_pct": None,
            "prevented_profitable_trade": None,
            "outcome_available": False,
            "missing_reason": "missing_local_bars",
        }
    timestamps = _bar_timestamps_utc(bars)
    if timestamps is None:
        return {
            "subsequent_intraday_return_pct": None,
            "max_gain_after_rejection_pct": None,
            "prevented_profitable_trade": None,
            "outcome_available": False,
            "missing_reason": "missing_bar_timestamps",
        }
    close_col = next((col for col in ("close", "Close", "c") if col in bars.columns), None)
    high_col = next((col for col in ("high", "High", "h") if col in bars.columns), None)
    low_col = next((col for col in ("low", "Low", "l") if col in bars.columns), None)
    if close_col is None:
        return {
            "subsequent_intraday_return_pct": None,
            "max_gain_after_rejection_pct": None,
            "prevented_profitable_trade": None,
            "outcome_available": False,
            "missing_reason": "missing_close_column",
        }
    work = bars.copy()
    work["_ts_utc"] = timestamps
    rejected_utc = rejected_at.astimezone(_UTC)
    same_day = work["_ts_utc"].dt.tz_convert(_ET).dt.date.astype(str) == day
    later = work.loc[same_day & (work["_ts_utc"] >= rejected_utc)].copy()
    if later.empty:
        return {
            "subsequent_intraday_return_pct": None,
            "max_gain_after_rejection_pct": None,
            "prevented_profitable_trade": None,
            "outcome_available": False,
            "missing_reason": "no_bars_after_rejection",
        }
    closes = pd.to_numeric(later[close_col], errors="coerce").dropna()
    if closes.empty:
        return {
            "subsequent_intraday_return_pct": None,
            "max_gain_after_rejection_pct": None,
            "prevented_profitable_trade": None,
            "outcome_available": False,
            "missing_reason": "missing_close_values",
        }
    base_price = rejected_price if rejected_price is not None and rejected_price > 0 else float(closes.iloc[0])
    eod_close = float(closes.iloc[-1])
    highs = pd.to_numeric(later[high_col], errors="coerce") if high_col else pd.to_numeric(later[close_col], errors="coerce")
    high_values = highs.dropna()
    max_high = float(high_values.max()) if not high_values.empty else eod_close
    max_high_rows = later.loc[highs == max_high] if not high_values.empty else pd.DataFrame()
    time_to_max_gain = (
        (max_high_rows.iloc[0]["_ts_utc"] - rejected_utc).total_seconds() / 60.0
        if not max_high_rows.empty
        else None
    )
    lows = pd.to_numeric(later[low_col], errors="coerce") if low_col else pd.to_numeric(later[close_col], errors="coerce")
    low_values = lows.dropna()
    min_low = float(low_values.min()) if not low_values.empty else eod_close
    first_15m = later.loc[later["_ts_utc"] <= rejected_utc + pd.Timedelta(minutes=15)]
    highs_15m = (
        pd.to_numeric(first_15m[high_col], errors="coerce").dropna()
        if high_col
        else pd.to_numeric(first_15m[close_col], errors="coerce").dropna()
    )
    max_gain_15m = _pct_return(float(highs_15m.max()), base_price) if not highs_15m.empty else None
    eod_return = ((eod_close / base_price) - 1.0) * 100.0
    max_gain = ((max_high / base_price) - 1.0) * 100.0
    max_drawdown = ((min_low / base_price) - 1.0) * 100.0
    return {
        "subsequent_intraday_return_pct": round(eod_return, 4),
        "max_gain_after_rejection_pct": round(max_gain, 4),
        "max_gain_after_block_pct": round(max_gain, 4),
        "time_to_max_gain_minutes": round(time_to_max_gain, 4) if time_to_max_gain is not None else None,
        "max_drawdown_after_block_pct": round(max_drawdown, 4),
        "return_after_5m_pct": _first_return_after_minutes(
            later,
            close_col=close_col,
            rejected_utc=rejected_utc,
            base_price=base_price,
            minutes=5,
        ),
        "return_after_10m_pct": _first_return_after_minutes(
            later,
            close_col=close_col,
            rejected_utc=rejected_utc,
            base_price=base_price,
            minutes=10,
        ),
        "return_after_15m_pct": _first_return_after_minutes(
            later,
            close_col=close_col,
            rejected_utc=rejected_utc,
            base_price=base_price,
            minutes=15,
        ),
        "return_after_30m_pct": _first_return_after_minutes(
            later,
            close_col=close_col,
            rejected_utc=rejected_utc,
            base_price=base_price,
            minutes=30,
        ),
        "return_after_60m_pct": _first_return_after_minutes(
            later,
            close_col=close_col,
            rejected_utc=rejected_utc,
            base_price=base_price,
            minutes=60,
        ),
        "reached_plus_1pct_within_15m": bool(max_gain_15m is not None and max_gain_15m >= 1.0),
        "reached_plus_2pct_within_15m": bool(max_gain_15m is not None and max_gain_15m >= 2.0),
        "reached_plus_3pct_within_15m": bool(max_gain_15m is not None and max_gain_15m >= 3.0),
        "prevented_profitable_trade": bool(max_gain > 0.0),
        "outcome_available": True,
        "missing_reason": None,
    }


def _mean(values: Sequence[float]) -> float | None:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return None
    return round(sum(finite) / len(finite), 4)


def _median(values: Sequence[float]) -> float | None:
    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return None
    mid = len(finite) // 2
    if len(finite) % 2:
        return round(finite[mid], 4)
    return round((finite[mid - 1] + finite[mid]) / 2.0, 4)


def _available_numbers(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = _safe_float(row.get(key))
        if value is not None:
            values.append(value)
    return values


def _percent_true(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    available = [row for row in rows if row.get("outcome_available")]
    if not available:
        return None
    return round((sum(1 for row in available if row.get(key) is True) / len(available)) * 100.0, 4)


def _compact_outcome(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "blocker": row.get("blocker"),
        "rejection_time": row.get("rejection_time"),
        "max_gain_after_block_pct": row.get("max_gain_after_block_pct"),
        "subsequent_intraday_return_pct": row.get("subsequent_intraday_return_pct"),
        "max_drawdown_after_block_pct": row.get("max_drawdown_after_block_pct"),
    }


def _best_worst(rows: Sequence[Mapping[str, Any]], key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    ranked = [row for row in rows if _safe_float(row.get(key)) is not None]
    if not ranked:
        return None, None
    best = max(ranked, key=lambda row: float(row.get(key)))
    worst = min(ranked, key=lambda row: float(row.get(key)))
    return _compact_outcome(best), _compact_outcome(worst)


def _build_blocker_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("blocker") or "unknown")].append(row)
    out: dict[str, dict[str, Any]] = {}
    for blocker, items in sorted(grouped.items()):
        max_gains = _available_numbers(items, "max_gain_after_block_pct")
        final_returns = _available_numbers(items, "subsequent_intraday_return_pct")
        out[blocker] = {
            "count": len(items),
            "outcomes_available": sum(1 for row in items if row.get("outcome_available")),
            "average_max_gain_pct": _mean(max_gains),
            "median_max_gain_pct": _median(max_gains),
            "average_final_return_pct": _mean(final_returns),
            "percent_reached_plus_1pct": _percent_true(items, "reached_plus_1pct_within_15m"),
            "percent_reached_plus_2pct": _percent_true(items, "reached_plus_2pct_within_15m"),
            "percent_reached_plus_3pct": _percent_true(items, "reached_plus_3pct_within_15m"),
        }
    return out


def _build_symbol_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("symbol") or "UNKNOWN")].append(row)
    out: dict[str, dict[str, Any]] = {}
    for symbol, items in sorted(grouped.items()):
        best, worst = _best_worst(items, "max_gain_after_block_pct")
        out[symbol] = {
            "occurrences": len(items),
            "outcomes_available": sum(1 for row in items if row.get("outcome_available")),
            "average_max_gain_pct": _mean(_available_numbers(items, "max_gain_after_block_pct")),
            "average_final_return_pct": _mean(_available_numbers(items, "subsequent_intraday_return_pct")),
            "average_drawdown_pct": _mean(_available_numbers(items, "max_drawdown_after_block_pct")),
            "best_outcome": best,
            "worst_outcome": worst,
        }
    return out


def _build_astn_section(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    astn_rows = [row for row in rows if str(row.get("symbol") or "").upper() == "ASTN"]
    return {
        "events": astn_rows,
        "occurrences": len(astn_rows),
        "outcomes_available": sum(1 for row in astn_rows if row.get("outcome_available")),
        "average_max_gain_pct": _mean(_available_numbers(astn_rows, "max_gain_after_block_pct")),
        "average_final_return_pct": _mean(_available_numbers(astn_rows, "subsequent_intraday_return_pct")),
        "average_drawdown_pct": _mean(_available_numbers(astn_rows, "max_drawdown_after_block_pct")),
        "average_time_to_max_gain_minutes": _mean(_available_numbers(astn_rows, "time_to_max_gain_minutes")),
        "percent_reached_plus_1pct": _percent_true(astn_rows, "reached_plus_1pct_within_15m"),
        "percent_reached_plus_2pct": _percent_true(astn_rows, "reached_plus_2pct_within_15m"),
        "percent_reached_plus_3pct": _percent_true(astn_rows, "reached_plus_3pct_within_15m"),
    }


def _discover_log_paths(project_root: Path, *, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    paths: list[Path] = []
    for base in (project_root / "data" / "logs", project_root / "reports" / "debug", project_root / "data" / "debug_logs"):
        if not base.exists():
            continue
        iterator = base.rglob("*") if base.name == "debug_logs" else base.glob("*")
        for path in iterator:
            if not path.is_file() or path.suffix not in {".log", ".txt", ".gz"}:
                continue
            path_day = _date_from_path(path)
            if path_day == day or day in str(path):
                paths.append(path)
    for raw in extra_paths or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _history_paths(data_dir: Path, *, day: str, user_id: str) -> list[Path]:
    root = data_dir / "dynamic_scan_history"
    if not root.exists():
        return []
    safe = _safe_user(user_id)
    exact: list[Path] = []
    fallback: list[Path] = []
    for path in root.glob("*.json"):
        if _date_from_path(path) != day:
            continue
        if path.name.endswith(f"_{safe}.json"):
            exact.append(path)
        elif path.name.endswith("_default.json"):
            fallback.append(path)
    return sorted(exact or fallback)


def _ingest_scan_history(
    *,
    data_dir: Path,
    day: str,
    user_id: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], list[str]]:
    selected: dict[str, list[dict[str, Any]]] = defaultdict(list)
    snapshots: dict[str, dict[str, Any]] = {}
    used: list[str] = []
    for path in _history_paths(data_dir, day=day, user_id=user_id):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        used.append(str(path))
        generated_at = _parse_timestamp(payload.get("generated_at"))
        selected_set = {str(sym).strip().upper() for sym in payload.get("selected") or [] if str(sym).strip()}
        for row in payload.get("candidates") or []:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            snap = {
                "symbol": symbol,
                "timestamp": (_parse_timestamp(row.get("timestamp")) or generated_at).isoformat()
                if (_parse_timestamp(row.get("timestamp")) or generated_at)
                else None,
                "price": _round(row.get("price")),
                "score": _round(row.get("score")),
                "source_file": str(path),
            }
            snapshots[symbol] = snap
            if symbol in selected_set:
                selected[symbol].append(snap)
    return selected, snapshots, used


def _ingest_logs(
    *,
    paths: Sequence[Path],
    day: str,
    selected: dict[str, list[dict[str, Any]]],
    snapshots: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    blockers: list[dict[str, Any]] = []
    used: list[str] = []
    pending_scan: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            lines = _read_text(path).splitlines()
        except Exception:
            continue
        used.append(str(path))
        for line_no, line in enumerate(lines, start=1):
            ts = _line_timestamp(line, day=day)
            scan = _SCAN_RE.search(line)
            if scan is not None:
                symbol = scan.group("symbol").upper()
                kv = _parse_kv(scan.group("body"))
                pending_scan[symbol] = {
                    "symbol": symbol,
                    "timestamp": ts.isoformat() if ts else None,
                    "price": _round(kv.get("price")),
                    "score": None,
                    "source_file": str(path),
                }
                snapshots[symbol] = {**snapshots.get(symbol, {}), **pending_scan[symbol]}
                continue
            selected_match = _SELECTED_RE.search(line)
            if selected_match is not None:
                symbol = selected_match.group("symbol").upper()
                kv = _parse_kv(line)
                row = {
                    "symbol": symbol,
                    "timestamp": ts.isoformat() if ts else None,
                    "price": snapshots.get(symbol, {}).get("price"),
                    "score": _round(kv.get("score")),
                    "source_file": str(path),
                    "line_number": line_no,
                }
                selected[symbol].append(row)
                snapshots[symbol] = {**snapshots.get(symbol, {}), **row}
                continue
            reason: str | None = None
            symbol = ""
            reject = _SCAN_REJECT_RE.search(line)
            if reject is not None:
                symbol = reject.group("symbol").upper()
                reason = reject.group("reason")
            else:
                skip = _SKIP_RE.search(line)
                if skip is not None:
                    symbol = skip.group("symbol").upper()
                    reason = skip.group("reason")
            if not symbol or reason is None:
                continue
            blocker = _normalize_blocker(reason)
            if blocker is None:
                continue
            scan_snapshot = pending_scan.get(symbol) or snapshots.get(symbol, {})
            blockers.append(
                {
                    "symbol": symbol,
                    "timestamp": ts.isoformat() if ts else scan_snapshot.get("timestamp"),
                    "price": scan_snapshot.get("price"),
                    "rejection_reason": reason.strip(),
                    "blocker": blocker,
                    "source_file": str(path),
                    "line_number": line_no,
                    "line": line.strip(),
                }
            )
    return blockers, used


def _selected_before_blocker(selected_rows: Sequence[Mapping[str, Any]], blocker: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if not selected_rows:
        return None
    blocked_at = _parse_timestamp(blocker.get("timestamp"))
    if blocked_at is None:
        return selected_rows[-1]
    before: list[Mapping[str, Any]] = []
    for row in selected_rows:
        selected_at = _parse_timestamp(row.get("timestamp"))
        if selected_at is None or selected_at <= blocked_at:
            before.append(row)
    return before[-1] if before else None


def build_dynamic_candidate_blockers_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build the selected-dynamic-candidate blocker report."""
    root = Path(project_root)
    data = Path(data_dir)
    selected, snapshots, history_used = _ingest_scan_history(data_dir=data, day=day, user_id=user_id)
    discovered_logs = _discover_log_paths(root, day=day, extra_paths=log_paths)
    blockers, logs_used = _ingest_logs(paths=discovered_logs, day=day, selected=selected, snapshots=snapshots)
    bars_cache: dict[str, tuple[pd.DataFrame | None, dict[str, Any]]] = {}
    rows: list[dict[str, Any]] = []
    selected_symbols = set(selected)
    for blocker in blockers:
        symbol = blocker["symbol"]
        selected_row = _selected_before_blocker(selected.get(symbol, []), blocker)
        if selected_row is None:
            continue
        rejected_at = _parse_timestamp(blocker.get("timestamp"))
        rejected_price = _safe_float(blocker.get("price")) or _safe_float(selected_row.get("price"))
        if symbol not in bars_cache:
            bars_cache[symbol] = _load_local_bars_with_diagnostics(
                data_dir=data,
                bars_dir=bars_dir,
                symbol=symbol,
                day=day,
            )
        bars, bar_diag = bars_cache[symbol]
        outcome = _outcome_from_bars(
            bars,
            rejected_at=rejected_at,
            rejected_price=rejected_price,
            day=day,
        )
        missing_bar_reason = bar_diag.get("missing_bar_reason")
        if outcome.get("missing_reason") == "missing_local_bars" and missing_bar_reason:
            outcome["missing_reason"] = missing_bar_reason
        for key in (
            "max_gain_after_block_pct",
            "time_to_max_gain_minutes",
            "max_drawdown_after_block_pct",
            "return_after_5m_pct",
            "return_after_10m_pct",
            "return_after_15m_pct",
            "return_after_30m_pct",
            "return_after_60m_pct",
            "reached_plus_1pct_within_15m",
            "reached_plus_2pct_within_15m",
            "reached_plus_3pct_within_15m",
        ):
            outcome.setdefault(key, None)
        row = {
            "symbol": symbol,
            "scanner_score": _round(selected_row.get("score")),
            "selected_time": selected_row.get("timestamp"),
            "rejection_time": blocker.get("timestamp"),
            "rejection_reason": blocker.get("rejection_reason"),
            "blocker": blocker.get("blocker"),
            "rejection_price": _round(rejected_price),
            "selected_candidate": True,
            "source_file": blocker.get("source_file"),
            "line_number": blocker.get("line_number"),
            "bar_diagnostics": bar_diag,
            "searched_paths": bar_diag.get("candidate_files", []),
            "first_available_bar_time": bar_diag.get("first_available_bar_time"),
            "last_available_bar_time": bar_diag.get("last_available_bar_time"),
            "missing_bar_reason": missing_bar_reason,
            **outcome,
        }
        rows.append(row)
    rows.sort(key=lambda row: (row.get("rejection_time") or "", row.get("symbol") or ""))
    symbol_bar_diagnostics = {
        symbol: diag
        for symbol, (_bars, diag) in sorted(bars_cache.items())
    }
    symbols_with_bars = sorted(
        symbol for symbol, (_bars, diag) in bars_cache.items() if diag.get("found_file")
    )
    symbols_missing_bars = sorted(
        symbol for symbol, (_bars, diag) in bars_cache.items() if not diag.get("found_file")
    )
    candidate_files_searched = sorted(
        {
            path
            for _symbol, (_bars, diag) in bars_cache.items()
            for path in (diag.get("candidate_files") or [])
        }
    )
    searched_paths = sorted(
        {
            path
            for _symbol, (_bars, diag) in bars_cache.items()
            for path in (diag.get("searched_roots") or [])
        }
    )
    blocker_summary = _build_blocker_summary(rows)
    symbol_summary = _build_symbol_summary(rows)
    astn_analysis = _build_astn_section(rows)
    return {
        "report": "dynamic_candidate_blockers",
        "research_only": True,
        "date": day,
        "user": user_id,
        "blocker_filters": ["short_history_need_200", "entry_alignment_breakout_new_high_orb"],
        "source_files": sorted(set(history_used + logs_used)),
        "selected_symbols": sorted(selected_symbols),
        "bar_diagnostics": symbol_bar_diagnostics,
        "candidates": rows,
        "blocker_summary": blocker_summary,
        "symbol_summary": symbol_summary,
        "astn_analysis": astn_analysis,
        "summary": {
            "selected_symbols": len(selected_symbols),
            "blocked_selected_candidates": len(rows),
            "short_history_need_200": sum(1 for row in rows if row.get("blocker") == "short_history_need_200"),
            "entry_alignment_breakout_new_high_orb": sum(
                1 for row in rows if row.get("blocker") == "entry_alignment_breakout_new_high_orb"
            ),
            "outcomes_available": sum(1 for row in rows if row.get("outcome_available")),
            "prevented_profitable_trade": sum(1 for row in rows if row.get("prevented_profitable_trade") is True),
            "missing_local_bars": sum(1 for row in rows if row.get("missing_reason") in {"missing_local_bars", "no_bar_roots_exist", "no_matching_bar_files", "no_matching_bars_for_date"}),
            "symbols_with_bars": symbols_with_bars,
            "symbols_missing_bars": symbols_missing_bars,
            "searched_paths": searched_paths,
            "candidate_files_searched": candidate_files_searched,
            "first_available_bar_time": min(
                (str(diag.get("first_available_bar_time")) for _sym, (_bars, diag) in bars_cache.items() if diag.get("first_available_bar_time")),
                default=None,
            ),
            "last_available_bar_time": max(
                (str(diag.get("last_available_bar_time")) for _sym, (_bars, diag) in bars_cache.items() if diag.get("last_available_bar_time")),
                default=None,
            ),
        },
    }


def render_dynamic_candidate_blockers_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Dynamic Candidate Blockers - {report.get('date')} user={report.get('user')}",
        "Research-only: no trading behavior, risk controls, entries, exits, allocator, sizing, or options logic changed.",
        "",
        "Summary",
        f"- selected symbols: {summary.get('selected_symbols', 0)}",
        f"- blocked selected candidates: {summary.get('blocked_selected_candidates', 0)}",
        f"- short_history_need_200: {summary.get('short_history_need_200', 0)}",
        f"- entry_alignment_breakout_new_high_orb: {summary.get('entry_alignment_breakout_new_high_orb', 0)}",
        f"- outcomes available: {summary.get('outcomes_available', 0)}",
        f"- prevented profitable trade: {summary.get('prevented_profitable_trade', 0)}",
        f"- missing local bars: {summary.get('missing_local_bars', 0)}",
        f"- symbols with bars: {', '.join(summary.get('symbols_with_bars') or []) or 'none'}",
        f"- symbols missing bars: {', '.join(summary.get('symbols_missing_bars') or []) or 'none'}",
        f"- first available bar time: {summary.get('first_available_bar_time')}",
        f"- last available bar time: {summary.get('last_available_bar_time')}",
        f"- searched paths: {len(summary.get('searched_paths') or [])}",
        "",
        "Blocked Candidates",
    ]
    rows = report.get("candidates") if isinstance(report.get("candidates"), list) else []
    if not rows:
        lines.append("- none")
    for row in rows:
        lines.append(
            "- {symbol} score={scanner_score} blocker={blocker} reason={rejection_reason} "
            "return={subsequent_intraday_return_pct} max_gain={max_gain_after_rejection_pct} "
            "time_to_max={time_to_max_gain_minutes}m drawdown={max_drawdown_after_block_pct} "
            "ret_5m={return_after_5m_pct} ret_15m={return_after_15m_pct} ret_60m={return_after_60m_pct} "
            "hit_1/2/3pct_15m={reached_plus_1pct_within_15m}/{reached_plus_2pct_within_15m}/{reached_plus_3pct_within_15m} "
            "prevented_profitable={prevented_profitable_trade} missing={missing_reason} "
            "missing_bar_reason={missing_bar_reason} bars={first_available_bar_time}->{last_available_bar_time}".format(**row)
        )
    blocker_summary = report.get("blocker_summary") if isinstance(report.get("blocker_summary"), Mapping) else {}
    if blocker_summary:
        lines.append("")
        lines.append("Blocker-Level Summary")
        for blocker, item in sorted(blocker_summary.items()):
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"- {blocker}: count={item.get('count')} avg_max_gain={item.get('average_max_gain_pct')} "
                f"median_max_gain={item.get('median_max_gain_pct')} avg_final_return={item.get('average_final_return_pct')} "
                f"hit_1/2/3pct={item.get('percent_reached_plus_1pct')}/{item.get('percent_reached_plus_2pct')}/{item.get('percent_reached_plus_3pct')}"
            )
    symbol_summary = report.get("symbol_summary") if isinstance(report.get("symbol_summary"), Mapping) else {}
    if symbol_summary:
        lines.append("")
        lines.append("Symbol-Level Summary")
        for symbol, item in sorted(symbol_summary.items()):
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"- {symbol}: occurrences={item.get('occurrences')} avg_max_gain={item.get('average_max_gain_pct')} "
                f"avg_final_return={item.get('average_final_return_pct')} avg_drawdown={item.get('average_drawdown_pct')} "
                f"best={item.get('best_outcome')} worst={item.get('worst_outcome')}"
            )
    astn = report.get("astn_analysis") if isinstance(report.get("astn_analysis"), Mapping) else {}
    if astn and astn.get("occurrences"):
        lines.append("")
        lines.append("ASTN Analysis")
        lines.append(
            f"- events={astn.get('occurrences')} outcomes_available={astn.get('outcomes_available')} "
            f"avg_max_gain={astn.get('average_max_gain_pct')} avg_final_return={astn.get('average_final_return_pct')} "
            f"avg_drawdown={astn.get('average_drawdown_pct')} avg_time_to_max={astn.get('average_time_to_max_gain_minutes')} "
            f"hit_1/2/3pct={astn.get('percent_reached_plus_1pct')}/{astn.get('percent_reached_plus_2pct')}/{astn.get('percent_reached_plus_3pct')}"
        )
        for event in astn.get("events") or []:
            if not isinstance(event, Mapping):
                continue
            lines.append(
                f"  - {event.get('rejection_time')} blocker={event.get('blocker')} max_gain={event.get('max_gain_after_block_pct')} "
                f"time_to_max={event.get('time_to_max_gain_minutes')} drawdown={event.get('max_drawdown_after_block_pct')} "
                f"ret_5/15/60={event.get('return_after_5m_pct')}/{event.get('return_after_15m_pct')}/{event.get('return_after_60m_pct')}"
            )
    diagnostics = report.get("bar_diagnostics") if isinstance(report.get("bar_diagnostics"), Mapping) else {}
    if diagnostics:
        lines.append("")
        lines.append("Bar Search Diagnostics")
        for symbol, diag in sorted(diagnostics.items()):
            lines.append(
                f"- {symbol} found={diag.get('found_file') or 'none'} format={diag.get('found_format')} "
                f"candidate_files={len(diag.get('candidate_files') or [])} "
                f"missing_bar_reason={diag.get('missing_bar_reason')}"
            )
    return "\n".join(lines) + "\n"


def write_dynamic_candidate_blockers_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    log_paths: Sequence[Path | str] | None = None,
    bars_dir: Path | str | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    report = build_dynamic_candidate_blockers_report(
        project_root=project_root,
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        log_paths=log_paths,
        bars_dir=bars_dir,
    )
    out_dir = Path(data_dir) / "research" / "dynamic_candidate_blockers"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{day}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_dynamic_candidate_blockers_report(report), encoding="utf-8")
    return json_path, text_path, report
