"""Read-only signal expectancy analytics."""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
import math
import re
import subprocess
import time as time_module
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.exposure import SYMBOL_SECTOR
from src.research_bars import CANONICAL_BAR_LOADER_SCHEMA_VERSION, expected_bar_dirs, load_canonical_bars
from src.trade_attribution import attribution_daily_path, load_daily_artifact

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_SYSLOG_RE = re.compile(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<clock>\d{2}:\d{2}:\d{2})\b")
_SCAN_REJECT_RE = re.compile(r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,12}):\s*(?P<body>.+)$")
_SCAN_LINE_RE = re.compile(r"\bDYNAMIC_SCAN\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,12}):\s*(?P<body>.+)$")
_SYMBOL_RE = re.compile(r"\bsymbol=([A-Z][A-Z0-9.\-]{0,12})\b")
_BACKFILL_1MIN_RE = re.compile(r"^(?P<symbol>[A-Z][A-Z0-9.\-]{0,12})_(?P<day>\d{4}-\d{2}-\d{2})_1Min\.csv$")
_HORIZONS = (1, 5, 10, 15, 30, 60)
_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6, "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
_SYMBOL_ONLY_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,12}$")
_SUSPICIOUS_LIVE_PREFIXES = ("replay-", "mock-", "test-", "shadow-", "paper-")
_SUSPICIOUS_LIVE_TOKENS = ("replay", "mock", "test", "shadow", "paper")
_PRIMARY_FORWARD_HORIZON = 15
_EXTREME_RETURN_THRESHOLD_PCT = 100.0
_EXTREME_RETURN_LOW_PRICE_THRESHOLD_PCT = 300.0
_MARKET_HOLIDAYS = {
    "2026-01-01",
    "2026-01-19",
    "2026-02-16",
    "2026-04-03",
    "2026-05-25",
    "2026-06-19",
    "2026-07-03",
    "2026-09-07",
    "2026-11-26",
    "2026-12-25",
}


def _day_text(day: date | str) -> str:
    return day.isoformat() if isinstance(day, date) else str(day)


def _compact_day(day: date | str) -> str:
    return _day_text(day).replace("-", "")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().strip("$,;%")
    if text.lower() in {"", "none", "n/a", "nan", "null"}:
        return None
    try:
        out = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _round(value: Any, ndigits: int = 4) -> float | None:
    number = _safe_float(value)
    return round(number, ndigits) if number is not None else None


def _parse_kv(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(text)}


def _date_from_compact(raw: str) -> str | None:
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 and raw.isdigit() else None


def _date_from_path(path: Path) -> str | None:
    for part in [path.name, *[parent.name for parent in path.parents]]:
        iso = _ISO_DATE_RE.search(part)
        if iso:
            return iso.group(1)
        compact = _COMPACT_DATE_RE.search(part)
        if compact:
            parsed = _date_from_compact(compact.group(1))
            if parsed:
                return parsed
    return None


def _line_matches_day(line: str, day: str) -> bool:
    return day in line or _compact_day(day) in line or not re.search(r"\d{4}-\d{2}-\d{2}", line)


def _parse_timestamp(value: Any, *, day: str) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        iso = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:.:\-+Z]+", text)
        if iso:
            text = iso.group(0)
        else:
            syslog = _SYSLOG_RE.search(text)
            if syslog:
                expected = datetime.strptime(day, "%Y-%m-%d").date()
                month = _MONTHS.get(syslog.group("mon"))
                if month != expected.month or int(syslog.group("day")) != expected.day:
                    return None
                hh, mm, ss = (int(part) for part in syslog.group("clock").split(":"))
                return datetime(expected.year, expected.month, expected.day, hh, mm, ss, tzinfo=_ET)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.replace(tzinfo=_ET) if dt.tzinfo is None else dt.astimezone(_ET)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(_ET).isoformat() if dt is not None else None


def _time_bucket(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    t = dt.astimezone(_ET).time()
    if time(9, 30) <= t < time(10, 0):
        return "09:30-10:00"
    if time(10, 0) <= t < time(11, 0):
        return "10:00-11:00"
    if time(11, 0) <= t < time(13, 0):
        return "11:00-13:00"
    if time(13, 0) <= t < time(15, 30):
        return "13:00-15:30"
    if time(15, 30) <= t <= time(16, 5):
        return "15:30-close"
    return "outside_market"


def _is_supported_signal_symbol(symbol: str) -> bool:
    return bool(symbol and _SYMBOL_ONLY_RE.match(symbol))


def _is_replay_like_record(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in (
            "environment",
            "record_origin",
            "route",
            "signal_type",
            "reason",
            "source",
            "source_name",
            "run_id",
            "session_id",
            "decision_id",
            "logical_order_id",
            "client_order_id",
            "broker_order_id",
            "broker_fill_id",
            "position_id",
            "order_id",
            "fill_id",
        )
    ).strip().lower()
    if any(prefix in text for prefix in _SUSPICIOUS_LIVE_PREFIXES):
        return True
    return any(re.search(rf"(^|[_\-\s:]){token}($|[_\-\s:])", text) for token in _SUSPICIOUS_LIVE_TOKENS)


def _session_failure_reason(ts: datetime | None, day: str) -> str | None:
    if ts is None:
        return "malformed_timestamp"
    event_day = ts.astimezone(_ET).date().isoformat()
    if event_day != day:
        return "event_date_out_of_scope"
    if ts.astimezone(_ET).weekday() >= 5:
        return "weekend"
    if event_day in _MARKET_HOLIDAYS:
        return "market_holiday"
    event_time = ts.astimezone(_ET).time()
    if event_time < time(4, 0) or event_time > time(20, 0):
        return "timestamp_outside_session"
    return None


def _failure_code(reason: str | None) -> str | None:
    if not reason:
        return None
    return "OUTCOME_UNAVAILABLE_" + str(reason).upper()


def _record_failure(debug: dict[str, Any], *, symbol: str, bucket: str, reason: str) -> None:
    debug.setdefault("lookup_failure_breakdown", Counter())[reason] += 1
    debug.setdefault("symbols_missing_bars_by_reason", defaultdict(set))[reason].add(symbol or "unknown")
    debug.setdefault("time_buckets_missing_bars", Counter())[bucket or "unknown"] += 1


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _discover_log_paths(project_root: Path, *, data_dir: Path, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    paths: list[Path] = []
    for root in (data_dir / "review" / day, data_dir / "logs", data_dir / "debug_logs", project_root / "logs", project_root / "reports" / "debug"):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".log", ".txt", ".out", ".gz"}:
                continue
            path_day = _date_from_path(path)
            if path_day == day or day in str(path) or _compact_day(day) in str(path):
                paths.append(path)
    for raw in extra_paths or []:
        path = Path(raw)
        if path.exists() and path.is_file():
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _journalctl_lines(day: str) -> list[str]:
    try:
        proc = subprocess.run(
            ["journalctl", "-u", "algo.service", "--since", f"{day} 00:00:00", "--until", f"{day} 23:59:59", "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def _load_log_lines(project_root: Path, data_dir: Path, *, day: str, log_text: str | None, log_files: Sequence[Path | str] | None) -> list[str]:
    if log_text is not None:
        return [line for line in log_text.splitlines() if _line_matches_day(line, day)]
    lines: list[str] = []
    for path in _discover_log_paths(project_root, data_dir=data_dir, day=day, extra_paths=log_files):
        try:
            lines.extend(_read_text(path).splitlines())
        except OSError:
            continue
    if not lines:
        lines.extend(_journalctl_lines(day))
    return [line for line in lines if _line_matches_day(line, day)]


def _symbol_from_line(line: str) -> str:
    match = _SYMBOL_RE.search(line)
    if match:
        return match.group(1).upper()
    scan = _SCAN_REJECT_RE.search(line) or _SCAN_LINE_RE.search(line)
    return scan.group("symbol").upper() if scan else ""


def _normalize_route(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values).lower()
    if "dynamic_momentum_override" in text:
        return "dynamic_momentum_override"
    if "weak_catalyst" in text:
        return "weak_catalyst_dynamic"
    if "core_rebuild" in text:
        return "core_rebuild"
    if "trend_long" in text:
        return "trend_long"
    if "dynamic" in text:
        return "dynamic_momentum_override"
    return str(next((value for value in values if value), "unknown")).strip() or "unknown"


def _signal_type(route: str, source: Any = None, reason: Any = None) -> str:
    text = f"{route} {source or ''} {reason or ''}".lower()
    if "weak_catalyst" in text:
        return "weak_catalyst_dynamic"
    if "entry_eval_pass" in text:
        return "entry_eval_pass"
    if "allocator" in text:
        return "allocator_action"
    if "submitted" in text:
        return "submitted_order"
    return route


def _catalyst_strength(row: Mapping[str, Any]) -> str:
    news = _safe_float(row.get("news_score")) or 0.0
    catalyst = _safe_float(row.get("catalyst_score")) or 0.0
    event = _safe_float(row.get("event_score")) or 0.0
    if news >= 7.0 or catalyst >= 0.7 or event >= 7.0:
        return "strong"
    if news > 0.0 or catalyst > 0.0 or event > 0.0:
        return "weak"
    return "none"


def _bucket_rvol(value: Any) -> str:
    rvol = _safe_float(value)
    if rvol is None:
        return "missing"
    if rvol < 0.5:
        return "<0.5"
    if rvol < 1.0:
        return "0.5-1.0"
    if rvol < 2.0:
        return "1.0-2.0"
    return ">2.0"


def _bucket_gain(value: Any) -> str:
    gain = _safe_float(value)
    if gain is None:
        return "missing"
    if gain < 0:
        return "<0%"
    if gain < 2:
        return "0-2%"
    if gain < 10:
        return "2-10%"
    return ">10%"


def _event_from_fields(
    *,
    symbol: str,
    ts: datetime | None,
    route: str,
    reason: str,
    source: str,
    fields: Mapping[str, Any],
    traded: bool = False,
) -> dict[str, Any]:
    price = _safe_float(fields.get("entry_price") or fields.get("filled_avg_price") or fields.get("price") or fields.get("paper_current_price") or fields.get("current_price"))
    rel = _safe_float(fields.get("relative_volume") or fields.get("rel_volume") or fields.get("rvol") or fields.get("scanner_relative_volume"))
    gain = _safe_float(fields.get("gain_pct") or fields.get("day_gain_pct") or fields.get("gain"))
    route_n = _normalize_route(route, fields.get("route"), fields.get("source"))
    row = {
        "symbol": symbol,
        "timestamp": _iso(ts),
        "_dt": ts,
        "route": route_n,
        "signal_type": _signal_type(route_n, source=source, reason=reason),
        "reason": reason,
        "source": source,
        "entry_price": _round(price),
        "relative_volume": _round(rel),
        "gain_pct": _round(gain),
        "news_score": _round(fields.get("news_score")),
        "catalyst_score": _round(fields.get("catalyst_score")),
        "event_score": _round(fields.get("event_score")),
        "market_regime": fields.get("market_regime") or fields.get("regime") or "missing",
        "sector": fields.get("sector") or SYMBOL_SECTOR.get(symbol, "unknown"),
        "time_bucket": _time_bucket(ts),
        "catalyst_strength": "none",
        "rvol_bucket": _bucket_rvol(rel),
        "gain_bucket": _bucket_gain(gain),
        "traded": bool(traded),
        "skipped": not bool(traded),
    }
    row["catalyst_strength"] = _catalyst_strength(row)
    return row


def _events_from_logs(lines: Sequence[str], *, day: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    allocator_actions: list[dict[str, Any]] = []
    for line in lines:
        ts = _parse_timestamp(line, day=day)
        if "ALLOCATOR ACTIONS:" in line:
            raw = line.split("ALLOCATOR ACTIONS:", 1)[1].strip()
            try:
                parsed = ast.literal_eval(re.sub(r"\b(?:nan|NaN|inf|-inf)\b", "None", raw))
            except Exception:
                parsed = []
            if isinstance(parsed, list):
                for row in parsed:
                    if isinstance(row, Mapping):
                        allocator_actions.append(dict(row))
                        sym = str(row.get("symbol") or "").strip().upper()
                        if sym:
                            events.append(_event_from_fields(symbol=sym, ts=ts, route=str(row.get("route") or row.get("source") or "allocator"), reason="allocator_action", source="allocator_actions", fields=row))
            continue
        kv = _parse_kv(line)
        symbol = _symbol_from_line(line)
        if not symbol:
            continue
        if _SCAN_LINE_RE.search(line) and "reject" not in line:
            events.append(_event_from_fields(symbol=symbol, ts=ts, route="dynamic_momentum_override", reason="scanner_candidate", source="dynamic_scan", fields=kv))
        elif _SCAN_REJECT_RE.search(line):
            reason = _SCAN_REJECT_RE.search(line).group("body").split(" ", 1)[0]
            events.append(_event_from_fields(symbol=symbol, ts=ts, route="dynamic_momentum_override", reason=reason, source="dynamic_scan_reject", fields=kv))
        elif "ENTRY_EVAL_PASS" in line:
            events.append(_event_from_fields(symbol=symbol, ts=ts, route=kv.get("route") or "entry_eval_pass", reason="entry_eval_pass", source="entry_eval", fields=kv))
        elif "ORDER_SKIP" in line and "weak_catalyst_dynamic_non_exceptional_live" in line:
            events.append(_event_from_fields(symbol=symbol, ts=ts, route="weak_catalyst_dynamic", reason="weak_catalyst_dynamic_skip", source="order_skip", fields=kv))
        elif "ORDER_SUBMITTED" in line or "ALLOCATOR_ACTION_SUBMITTED" in line:
            events.append(_event_from_fields(symbol=symbol, ts=ts, route=kv.get("route") or "submitted_order", reason="submitted_order", source="order_submitted", fields=kv, traded=True))
    return events


def _attribution_payload(data_dir: Path, *, user_id: str, day: str) -> dict[str, Any]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    return load_daily_artifact(path)


def _events_from_attribution(payload: Mapping[str, Any], *, day: str) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    events: list[dict[str, Any]] = []
    exits_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload.get("exits", []) if isinstance(payload.get("exits"), list) else []:
        if not isinstance(row, Mapping):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            exits_by_symbol[sym].append(dict(row))
    for row in payload.get("candidates", []) if isinstance(payload.get("candidates"), list) else []:
        if not isinstance(row, Mapping):
            continue
        if _is_replay_like_record(row):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            events.append(_event_from_fields(symbol=sym, ts=_parse_timestamp(row.get("timestamp"), day=day), route=str(row.get("route") or row.get("source") or "candidate"), reason=str(row.get("reason") or "candidate"), source="attribution_candidate", fields=row))
    for row in payload.get("allocator_candidates", []) if isinstance(payload.get("allocator_candidates"), list) else []:
        if not isinstance(row, Mapping):
            continue
        if _is_replay_like_record(row):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if sym:
            events.append(_event_from_fields(symbol=sym, ts=_parse_timestamp(row.get("timestamp"), day=day), route=str(row.get("route") or row.get("source") or "allocator_candidate"), reason="allocator_candidate", source="attribution_allocator", fields=row))
    for row in payload.get("orders", []) if isinstance(payload.get("orders"), list) else []:
        if not isinstance(row, Mapping):
            continue
        if _is_replay_like_record(row):
            continue
        sym = str(row.get("symbol") or "").strip().upper()
        if not sym:
            continue
        traded = bool(row.get("submitted"))
        event = _event_from_fields(symbol=sym, ts=_parse_timestamp(row.get("timestamp"), day=day), route=str(row.get("route") or row.get("source") or "order"), reason="submitted_order" if traded else str(row.get("reject_reason") or "order_skip"), source="attribution_order", fields=row, traded=traded)
        event["realized_pnl"] = None
        event["hold_minutes"] = None
        event["exit_reason"] = None
        if traded and exits_by_symbol.get(sym):
            exit_row = exits_by_symbol[sym][0]
            event["realized_pnl"] = _round(exit_row.get("pnl"))
            event["pnl_pct"] = _round(exit_row.get("pnl_pct"))
            event["hold_minutes"] = _round(exit_row.get("hold_minutes"))
            event["exit_reason"] = exit_row.get("exit_reason")
        events.append(event)
    return events, exits_by_symbol


def _candidate_bar_files(data_dir: Path, symbol: str, day: str, bars_dir: Path | None) -> list[Path]:
    roots = _bar_roots(data_dir, bars_dir)
    compact = day.replace("-", "")
    patterns = [
        f"{symbol}_{day}_1Min.csv",
        f"**/{symbol}_{day}_1Min.csv",
        f"**/{symbol}*{day}*.csv",
        f"**/{day}*{symbol}*.csv",
        f"**/{symbol}*{compact}*.csv",
        f"**/{compact}*{symbol}*.csv",
        f"**/{day}/**/{symbol}.csv",
        f"**/{compact}/**/{symbol}.csv",
    ]
    paths: list[Path] = []
    for root in roots:
        if root is None or not root.exists():
            continue
        for pattern in patterns:
            paths.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(dict.fromkeys(paths))


def _bar_roots(data_dir: Path, bars_dir: Path | None) -> list[Path]:
    if bars_dir is not None:
        return [bars_dir]
    roots = [*expected_bar_dirs(data_dir), data_dir / "research" / "dynamic_candidate_bars"]
    return list(dict.fromkeys(roots))


def _normalize_bar_columns(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    work.columns = [str(col).strip().lower() for col in work.columns]
    return work


def _infer_backfill_1min_timestamps(path: Path, frame: pd.DataFrame, day: str) -> pd.Series | None:
    match = _BACKFILL_1MIN_RE.match(path.name)
    if not match or match.group("day") != day:
        return None
    start = pd.Timestamp(f"{day} 09:30:00", tz=_ET)
    return pd.Series(pd.date_range(start=start, periods=len(frame), freq="min").tz_convert(_UTC), index=frame.index)


def _load_bars(
    data_dir: Path,
    *,
    symbol: str,
    day: str,
    bars_dir: Path | None,
    cache: dict[str, pd.DataFrame | None],
    debug: dict[str, Any],
) -> pd.DataFrame | None:
    lookup_start = time_module.perf_counter()
    symbol_debug = debug.setdefault("bar_lookup_by_symbol", {}).setdefault(
        symbol,
        {
            "symbol": symbol,
            "cache_status": "miss",
            "candidate_files": [],
            "files_found": [],
            "bars_loaded": 0,
            "earliest_available_bar": None,
            "latest_available_bar": None,
            "source_selected": None,
            "persistence_status": "unknown",
            "failure_reason": None,
            "lookup_latency_ms": None,
        },
    )
    if symbol in cache:
        symbol_debug["cache_status"] = "hit"
        debug["loader_cache_hits"] = int(debug.get("loader_cache_hits") or 0) + 1
        symbol_debug["lookup_latency_ms"] = round((time_module.perf_counter() - lookup_start) * 1000.0, 3)
        return cache[symbol]
    debug["loader_cache_misses"] = int(debug.get("loader_cache_misses") or 0) + 1
    bars, meta = load_canonical_bars(data_dir, symbol=symbol, day=day, bars_dir=bars_dir)
    roots = _bar_roots(data_dir, bars_dir)
    paths = [Path(path) for path in (meta.get("candidate_files") or [])]
    symbol_debug["candidate_files"] = [str(path) for path in paths]
    symbol_debug["files_found"] = list(meta.get("files_found") or meta.get("candidate_files") or [])
    symbol_debug["file_inspections"] = meta.get("file_inspections") or []
    symbol_debug["source_selected"] = meta.get("source_selected")
    debug["file_lookup_attempts"] = int(debug.get("file_lookup_attempts") or 0) + int(meta.get("file_lookup_attempts") or 0)
    debug["file_exists_hits"] = int(debug.get("file_exists_hits") or 0) + int(meta.get("file_exists_hits") or 0)
    debug["missing_file_count"] = int(debug.get("missing_file_count") or 0) + int(meta.get("missing_file_count") or 0)
    debug["valid_parsed_file_hits"] = int(debug.get("valid_parsed_file_hits") or 0) + int(meta.get("valid_parsed_file_hits") or 0)
    debug["invalid_file_hits"] = int(debug.get("invalid_file_hits") or 0) + int(meta.get("invalid_file_hits") or 0)
    debug.setdefault("bars_files_found", [])
    debug.setdefault("symbols_with_bars", [])
    for path in paths:
        debug["bars_files_found"].append(str(path))
    if bars is None or bars.empty:
        cache[symbol] = None
        reason = str(meta.get("reason") or "no_historical_source")
        persistence = str(meta.get("persistence_status") or "bar_files_corrupted")
        symbol_debug["failure_reason"] = reason
        symbol_debug["persistence_status"] = persistence
        symbol_debug["lookup_latency_ms"] = round((time_module.perf_counter() - lookup_start) * 1000.0, 3)
        return None
    cache[symbol] = bars
    debug["symbols_with_bars"].append(symbol)
    symbol_debug["bars_loaded"] = int(len(bars))
    symbol_debug["earliest_available_bar"] = meta.get("first_timestamp")
    symbol_debug["latest_available_bar"] = meta.get("last_timestamp")
    symbol_debug["persistence_status"] = "loaded"
    symbol_debug["lookup_latency_ms"] = round((time_module.perf_counter() - lookup_start) * 1000.0, 3)
    return cache[symbol]


def _first_close_at_or_after(bars: pd.DataFrame, ts: datetime) -> float | None:
    matches = bars.loc[bars["_timestamp"] >= pd.Timestamp(ts.astimezone(_UTC))]
    if matches.empty:
        return None
    return _safe_float(matches.iloc[0].get("close"))


def _first_bar_at_or_after(bars: pd.DataFrame, ts: datetime) -> Mapping[str, Any] | None:
    matches = bars.loc[bars["_timestamp"] >= pd.Timestamp(ts.astimezone(_UTC))]
    if matches.empty:
        return None
    return matches.iloc[0]


def _extreme_return_threshold(entry_price: float) -> float:
    return _EXTREME_RETURN_LOW_PRICE_THRESHOLD_PCT if entry_price < 5.0 else _EXTREME_RETURN_THRESHOLD_PCT


def _validated_return_pct(*, entry: float, future: float | None) -> tuple[float | None, str | None]:
    if entry <= 0:
        return None, "ZERO_OR_INVALID_REFERENCE_PRICE"
    if future is None or future <= 0:
        return None, "ZERO_OR_INVALID_FUTURE_PRICE"
    value = ((future - entry) / entry) * 100.0
    if not math.isfinite(value):
        return None, "NONFINITE_RETURN"
    if abs(value) > _extreme_return_threshold(entry):
        return None, "EXTREME_RETURN_UNCORROBORATED"
    return _round(value), None


def _attach_forward_returns(rows: list[dict[str, Any]], *, data_dir: Path, day: str, bars_dir: Path | None) -> dict[str, Any]:
    cache: dict[str, pd.DataFrame | None] = {}
    debug: dict[str, Any] = {
        "bars_directories_checked": [str(path) for path in _bar_roots(data_dir, bars_dir)],
        "bars_files_found": [],
        "symbols_with_bars": [],
        "missing_symbols": [],
        "lookup_failure_breakdown": Counter(),
        "symbols_missing_bars_by_reason": defaultdict(set),
        "time_buckets_missing_bars": Counter(),
        "bar_lookup_by_symbol": {},
        "cache_hits": 0,
        "cache_misses": 0,
        "file_lookup_attempts": 0,
        "file_exists_hits": 0,
        "missing_file_count": 0,
        "valid_parsed_file_hits": 0,
        "invalid_file_hits": 0,
        "loader_cache_hits": 0,
        "loader_cache_misses": 0,
        "signal_forward_lookup_attempts": 0,
        "signal_forward_lookup_successes": 0,
        "signal_forward_lookup_failures": 0,
        "forward_horizon_valid_counts": Counter(),
        "forward_horizon_missing_counts": Counter(),
        "anomalous_observations": 0,
        "anomaly_breakdown": Counter(),
        "excluded_observations": 0,
    }
    missing_bars = 0
    missing_entry = 0
    signals_with_valid_forward_bars = 0
    missing_symbols: set[str] = set()
    for row in rows:
        sym = str(row.get("symbol") or "").strip().upper()
        ts = row.get("_dt") if isinstance(row.get("_dt"), datetime) else _parse_timestamp(row.get("timestamp"), day=day)
        row["forward_lookup_status"] = "unavailable"
        row["forward_lookup_failure_reason"] = None
        row["forward_lookup_unavailable_code"] = None
        row["forward_lookup_source"] = None
        row["forward_lookup_cache_status"] = None
        row["forward_validation_status"] = "UNAVAILABLE"
        row["forward_validation_reasons"] = []
        if ts is None:
            missing_entry += 1
            missing_bars += 1
            reason = "malformed_timestamp"
            row["forward_lookup_failure_reason"] = reason
            row["forward_lookup_unavailable_code"] = _failure_code(reason)
            _record_failure(debug, symbol=sym, bucket=_time_bucket(None), reason=reason)
            if sym:
                missing_symbols.add(sym)
            continue
        session_reason = _session_failure_reason(ts, day)
        if session_reason:
            missing_bars += 1
            row["forward_lookup_failure_reason"] = session_reason
            row["forward_lookup_unavailable_code"] = _failure_code(session_reason)
            _record_failure(debug, symbol=sym, bucket=_time_bucket(ts), reason=session_reason)
            if sym:
                missing_symbols.add(sym)
            continue
        if not _is_supported_signal_symbol(sym):
            missing_bars += 1
            reason = "invalid_symbol"
            row["forward_lookup_failure_reason"] = reason
            row["forward_lookup_unavailable_code"] = _failure_code(reason)
            _record_failure(debug, symbol=sym, bucket=_time_bucket(ts), reason=reason)
            if sym:
                missing_symbols.add(sym)
            continue
        debug["signal_forward_lookup_attempts"] = int(debug.get("signal_forward_lookup_attempts") or 0) + 1
        bars = _load_bars(data_dir, symbol=sym, day=day, bars_dir=bars_dir, cache=cache, debug=debug) if sym else None
        lookup_info = (debug.get("bar_lookup_by_symbol") or {}).get(sym) or {}
        row["forward_lookup_cache_status"] = lookup_info.get("cache_status")
        row["forward_lookup_source"] = lookup_info.get("source_selected")
        entry = _safe_float(row.get("entry_price"))
        if entry is None and bars is not None:
            entry = _first_close_at_or_after(bars, ts)
            row["entry_price"] = _round(entry)
        if bars is None or bars.empty:
            missing_bars += 1
            debug["signal_forward_lookup_failures"] = int(debug.get("signal_forward_lookup_failures") or 0) + 1
            reason = str(lookup_info.get("failure_reason") or "no_historical_source")
            row["forward_lookup_failure_reason"] = reason
            row["forward_lookup_unavailable_code"] = _failure_code(reason)
            _record_failure(debug, symbol=sym, bucket=_time_bucket(ts), reason=reason)
            if sym:
                missing_symbols.add(sym)
            continue
        if entry is None or entry <= 0:
            missing_entry += 1
            missing_bars += 1
            debug["signal_forward_lookup_failures"] = int(debug.get("signal_forward_lookup_failures") or 0) + 1
            reason = "missing_entry_context"
            row["forward_lookup_failure_reason"] = reason
            row["forward_lookup_unavailable_code"] = _failure_code(reason)
            _record_failure(debug, symbol=sym, bucket=_time_bucket(ts), reason=reason)
            continue
        row_valid = False
        row_anomalies: list[str] = []
        for minutes in _HORIZONS:
            horizon_ts = ts + pd.Timedelta(minutes=minutes).to_pytimedelta()
            future_bar = _first_bar_at_or_after(bars, horizon_ts)
            close = _safe_float(future_bar.get("close")) if future_bar is not None else None
            if future_bar is None:
                value, anomaly = None, None
            else:
                value, anomaly = _validated_return_pct(entry=entry, future=close)
            row[f"return_{minutes}m_pct"] = value
            row[f"forward_return_{minutes}m"] = value
            row[f"forward_window_{minutes}m_valid"] = value is not None
            if future_bar is not None:
                row[f"forward_bar_{minutes}m_timestamp"] = pd.Timestamp(future_bar.get("_timestamp")).isoformat()
            if anomaly:
                row_anomalies.append(anomaly)
                debug["anomaly_breakdown"][anomaly] += 1
            if value is None:
                debug["forward_horizon_missing_counts"][f"{minutes}m"] += 1
            else:
                debug["forward_horizon_valid_counts"][f"{minutes}m"] += 1
            if value is not None:
                row_valid = row_valid or minutes == _PRIMARY_FORWARD_HORIZON
        window = bars.loc[(bars["_timestamp"] >= pd.Timestamp(ts.astimezone(_UTC))) & (bars["_timestamp"] <= pd.Timestamp((ts + pd.Timedelta(minutes=60).to_pytimedelta()).astimezone(_UTC)))]
        if not window.empty:
            high = _safe_float(window["high"].max()) if "high" in window.columns else _safe_float(window["close"].max())
            low = _safe_float(window["low"].min()) if "low" in window.columns else _safe_float(window["close"].min())
            mfe, mfe_anomaly = _validated_return_pct(entry=entry, future=high)
            mae, mae_anomaly = _validated_return_pct(entry=entry, future=low)
            if mfe is not None and mae is not None and mfe < mae:
                row_anomalies.append("MFE_BELOW_MAE")
                debug["anomaly_breakdown"]["MFE_BELOW_MAE"] += 1
                mfe = None
                mae = None
            for anomaly in (mfe_anomaly, mae_anomaly):
                if anomaly:
                    row_anomalies.append(anomaly)
                    debug["anomaly_breakdown"][anomaly] += 1
            row["max_favorable_excursion_pct"] = mfe
            row["max_adverse_excursion_pct"] = mae
        else:
            row_anomalies.append("INSUFFICIENT_FORWARD_WINDOW")
            debug["anomaly_breakdown"]["INSUFFICIENT_FORWARD_WINDOW"] += 1
        if row_anomalies:
            row["forward_validation_reasons"] = sorted(set(row_anomalies))
            debug["anomalous_observations"] = int(debug.get("anomalous_observations") or 0) + 1
            if not row_valid:
                debug["excluded_observations"] = int(debug.get("excluded_observations") or 0) + 1
        if row_valid:
            signals_with_valid_forward_bars += 1
            debug["signal_forward_lookup_successes"] = int(debug.get("signal_forward_lookup_successes") or 0) + 1
            row["forward_lookup_status"] = "available"
            row["forward_validation_status"] = "VALID" if not row_anomalies else "VALID_WITH_EXCLUDED_ANOMALIES"
        else:
            missing_bars += 1
            debug["signal_forward_lookup_failures"] = int(debug.get("signal_forward_lookup_failures") or 0) + 1
            reason = row_anomalies[0].lower() if row_anomalies else "insufficient_forward_window"
            row["forward_lookup_failure_reason"] = reason
            row["forward_lookup_unavailable_code"] = _failure_code(reason)
            _record_failure(debug, symbol=sym, bucket=_time_bucket(ts), reason=reason)
            missing_symbols.add(sym)
    debug["bars_files_found"] = sorted(dict.fromkeys(debug["bars_files_found"]))
    debug["symbols_with_bars"] = sorted(set(debug["symbols_with_bars"]))
    debug["missing_symbols"] = sorted(missing_symbols)
    lookup_details = list((debug.get("bar_lookup_by_symbol") or {}).values())
    earliest = sorted([str(item.get("earliest_available_bar")) for item in lookup_details if item.get("earliest_available_bar")])
    latest = sorted([str(item.get("latest_available_bar")) for item in lookup_details if item.get("latest_available_bar")])
    failure_breakdown = dict(sorted((debug.get("lookup_failure_breakdown") or {}).items()))
    anomaly_breakdown = dict(sorted((debug.get("anomaly_breakdown") or {}).items()))
    symbols_by_reason = {reason: sorted(symbols) for reason, symbols in sorted((debug.get("symbols_missing_bars_by_reason") or {}).items())}
    time_buckets = dict(sorted((debug.get("time_buckets_missing_bars") or {}).items()))
    source_selected = sorted({str(item.get("source_selected")) for item in lookup_details if item.get("source_selected")})
    latency_values = [_safe_float(item.get("lookup_latency_ms")) for item in lookup_details]
    latency_values = [value for value in latency_values if value is not None]
    persistence_statuses = Counter(str(item.get("persistence_status") or "unknown") for item in lookup_details)
    return {
        **debug,
        "missing_bars": missing_bars,
        "missing_entry_context": missing_entry,
        "signals_with_valid_forward_bars": signals_with_valid_forward_bars,
        "lookup_success_rate": round(signals_with_valid_forward_bars / len(rows), 4) if rows else None,
        "lookup_failure_breakdown": failure_breakdown,
        "symbols_missing_bars": sorted(missing_symbols),
        "symbols_missing_bars_by_reason": symbols_by_reason,
        "time_buckets_missing_bars": time_buckets,
        "earliest_available_bar": earliest[0] if earliest else None,
        "latest_available_bar": latest[-1] if latest else None,
        "source_selected": source_selected,
        "cache_hits": max(
            int(debug.get("loader_cache_hits") or 0),
            max(0, len(rows) - int(debug.get("loader_cache_misses") or 0)),
        ),
        "cache_misses": int(debug.get("loader_cache_misses") or 0),
        "file_lookup_attempts": int(debug.get("file_lookup_attempts") or 0),
        "file_exists_hits": int(debug.get("file_exists_hits") or 0),
        "missing_file_count": int(debug.get("missing_file_count") or 0),
        "valid_parsed_file_hits": int(debug.get("valid_parsed_file_hits") or 0),
        "invalid_file_hits": int(debug.get("invalid_file_hits") or 0),
        "loader_cache_hits": int(debug.get("loader_cache_hits") or 0),
        "loader_cache_misses": int(debug.get("loader_cache_misses") or 0),
        "signal_forward_lookup_attempts": int(debug.get("signal_forward_lookup_attempts") or 0),
        "signal_forward_lookup_successes": int(debug.get("signal_forward_lookup_successes") or 0),
        "signal_forward_lookup_failures": int(debug.get("signal_forward_lookup_failures") or 0),
        "forward_horizon_valid_counts": dict(sorted((debug.get("forward_horizon_valid_counts") or {}).items())),
        "forward_horizon_missing_counts": dict(sorted((debug.get("forward_horizon_missing_counts") or {}).items())),
        "anomalous_observations": int(debug.get("anomalous_observations") or 0),
        "excluded_observations": int(debug.get("excluded_observations") or 0),
        "anomaly_breakdown": anomaly_breakdown,
        "return_unit": "percent_points",
        "internal_return_unit": "decimal_fraction_converted_to_percent_points_for_report",
        "loader_schema_version": CANONICAL_BAR_LOADER_SCHEMA_VERSION,
        "lookup_latency_ms_total": round(sum(latency_values), 3) if latency_values else 0.0,
        "lookup_latency_ms_avg": round(sum(latency_values) / len(latency_values), 3) if latency_values else None,
        "persistence_status": dict(sorted(persistence_statuses.items())),
        "lookup_failure_breakdown": failure_breakdown,
        "symbols_missing_bars_by_reason": symbols_by_reason,
        "time_buckets_missing_bars": time_buckets,
    }


def _dedupe(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = (str(row.get("timestamp") or ""), str(row.get("symbol") or ""), str(row.get("route") or ""), str(row.get("source") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _profit_factor(pnls: Sequence[float]) -> float | None:
    wins = sum(v for v in pnls if v > 0)
    losses = abs(sum(v for v in pnls if v < 0))
    if losses == 0:
        return round(wins, 4) if wins > 0 else None
    return round(wins / losses, 4)


def _expectancy_score(rows: Sequence[Mapping[str, Any]]) -> float:
    fwd = [v for row in rows if (v := _safe_float(row.get("return_15m_pct"))) is not None]
    pnl = [v for row in rows if (v := _safe_float(row.get("realized_pnl"))) is not None]
    win_rate = len([v for v in pnl if v > 0]) / len(pnl) if pnl else 0.0
    return round((_mean(fwd) or 0.0) + win_rate + ((_profit_factor(pnl) or 0.0) * 0.1), 4)


def _summary_row(name: str, rows: Sequence[Mapping[str, Any]], *, field_name: str) -> dict[str, Any]:
    pnls = [v for row in rows if (v := _safe_float(row.get("realized_pnl"))) is not None]
    returns = {m: [v for row in rows if (v := _safe_float(row.get(f"return_{m}m_pct"))) is not None] for m in _HORIZONS}
    wins = [v for v in pnls if v > 0]
    losses = [v for v in pnls if v < 0]
    return {
        field_name: name,
        "count": len(rows),
        "trades": sum(1 for row in rows if row.get("traded")),
        "skipped": sum(1 for row in rows if not row.get("traded")),
        **{f"avg_{m}m_return": _mean(vals) for m, vals in returns.items()},
        "avg_forward_return": _mean([v for vals in returns.values() for v in vals]),
        "realized_pnl": round(sum(pnls), 4) if pnls else None,
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "average_win": _mean(wins),
        "average_loss": _mean(losses),
        "profit_factor": _profit_factor(pnls),
        "max_adverse_excursion": min([v for row in rows if (v := _safe_float(row.get("max_adverse_excursion_pct"))) is not None], default=None),
        "max_favorable_excursion": max([v for row in rows if (v := _safe_float(row.get("max_favorable_excursion_pct"))) is not None], default=None),
        "average_hold_time": _mean([v for row in rows if (v := _safe_float(row.get("hold_minutes"))) is not None]),
        "stop_loss_rate": round(sum(1 for row in rows if "stop_loss" in str(row.get("exit_reason") or "")) / len(rows), 4) if rows else None,
        "weak_exit_rate": round(sum(1 for row in rows if "weak" in str(row.get("exit_reason") or "")) / len(rows), 4) if rows else None,
        "expectancy_score": _expectancy_score(rows),
    }


def _group(rows: Sequence[Mapping[str, Any]], key: str, out_key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "missing")].append(row)
    return sorted((_summary_row(name, group_rows, field_name=out_key) for name, group_rows in grouped.items()), key=lambda r: float(r.get("expectancy_score") or 0.0), reverse=True)


def _symbol_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("symbol") or ""), str(row.get("route") or ""))].append(row)
    out = []
    for (symbol, route), group_rows in grouped.items():
        row = _summary_row(symbol, group_rows, field_name="symbol")
        row["route"] = route
        score = float(row.get("expectancy_score") or 0.0)
        row["recommendation"] = "increase_review" if score > 1.0 else "reduce_review" if score < -0.5 else "hold"
        out.append(row)
    return sorted(out, key=lambda r: float(r.get("expectancy_score") or 0.0), reverse=True)


def _recommendations(route_rows: Sequence[Mapping[str, Any]], symbol_rows: Sequence[Mapping[str, Any]], time_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    recs: list[str] = []
    for row in route_rows:
        score = float(row.get("expectancy_score") or 0.0)
        route = row.get("route")
        if score > 1.0:
            recs.append(f"increase size review for high expectancy route {route}")
        elif score < -0.5:
            recs.append(f"reduce/disable review for low expectancy route {route}")
    for row in symbol_rows[-3:]:
        if float(row.get("expectancy_score") or 0.0) < -0.5:
            recs.append(f"avoid or review {row.get('symbol')} on {row.get('route')}")
    for row in time_rows:
        if float(row.get("expectancy_score") or 0.0) < -0.5:
            recs.append(f"avoid/reduce signals during {row.get('time_bucket')}")
    if not recs:
        recs.append("No strong positive/negative expectancy pattern found with available data.")
    return recs[:10]


def _blocked_recommendations(reasons: Sequence[str]) -> list[str]:
    return [f"recommendation_status: BLOCKED_DATA_QUALITY ({', '.join(reasons)})"]


def _dynamic_momentum_gate_suggestions(
    route_rows: Sequence[Mapping[str, Any]],
    symbol_rows: Sequence[Mapping[str, Any]],
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    route = next((row for row in route_rows if row.get("route") == "dynamic_momentum_override"), None)
    dynamic_symbols = [
        row
        for row in symbol_rows
        if row.get("route") == "dynamic_momentum_override"
        and int(_safe_float(row.get("count")) or 0) >= min_samples
        and float(row.get("expectancy_score") or 0.0) < 0.0
    ]
    blocked = [
        str(row.get("symbol") or "")
        for row in dynamic_symbols
        if float(row.get("expectancy_score") or 0.0) <= -1.0
    ]
    reduced = [
        str(row.get("symbol") or "")
        for row in dynamic_symbols
        if str(row.get("symbol") or "") not in set(blocked)
    ]
    route_score = None if route is None else _round(route.get("expectancy_score"))
    route_count = 0 if route is None else int(_safe_float(route.get("count")) or 0)
    return {
        "research_only": True,
        "route": "dynamic_momentum_override",
        "route_expectancy_score": route_score,
        "route_sample_count": route_count,
        "route_level_recommendation": (
            "cap_negative_expectancy_symbols"
            if route is not None and route_count >= min_samples and float(route.get("expectancy_score") or 0.0) < 0.0
            else "hold_current_gate"
        ),
        "candidate_reduced_symbols": sorted(reduced),
        "candidate_blocked_symbols": sorted(blocked),
        "suggested_config": {
            "dynamic_universe": {
                "dynamic_momentum_expectancy_gate": {
                    "enabled": True,
                    "live_enabled": True,
                    "min_expectancy_score": 0.0,
                    "lookback_days": 5,
                    "min_samples": min_samples,
                    "reduce_only_when_negative": True,
                    "fallback_allow_if_no_data": True,
                    "blocked_symbols": sorted(blocked),
                    "reduced_symbols": sorted(reduced),
                    "max_notional_when_negative": 300,
                }
            }
        },
    }


def build_signal_expectancy_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    bars_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Build signal expectancy analytics without trading side effects."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    bars_path = Path(bars_dir) if bars_dir is not None else None
    lines = _load_log_lines(root, data, day=day_s, log_text=log_text, log_files=log_files)
    payload = _attribution_payload(data, user_id=user_id, day=day_s)
    attr_events, exits_by_symbol = _events_from_attribution(payload, day=day_s)
    rows = _dedupe([*_events_from_logs(lines, day=day_s), *attr_events])
    if str(user_id or "").strip() == "live_bot":
        rows = [row for row in rows if not _is_replay_like_record(row)]
    quality = _attach_forward_returns(rows, data_dir=data, day=day_s, bars_dir=bars_path)
    source_files = sorted(str(path) for path in (quality.get("bars_files_found") or []))
    source_stats: list[dict[str, Any]] = []
    for path_text in source_files:
        path = Path(path_text)
        try:
            stat = path.stat()
        except OSError:
            continue
        source_stats.append({"path": path_text, "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)})
    try:
        git_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False).stdout.strip() or None
    except Exception:
        git_commit = None
    input_fingerprint = hashlib.sha256(
        json.dumps({"day": day_s, "user": user_id, "sources": source_stats, "signals": len(rows)}, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    route_rows = _group(rows, "route", "route")
    symbol_rows = _symbol_rows(rows)
    time_rows = _group(rows, "time_bucket", "time_bucket")
    quality_block_reasons: list[str] = []
    if int(quality.get("anomalous_observations") or 0) > 0:
        quality_block_reasons.append("anomalous_return_observations")
    if int(quality.get("excluded_observations") or 0) > 0:
        quality_block_reasons.append("excluded_return_observations")
    if int(quality.get("signals_with_valid_forward_bars") or 0) < 30:
        quality_block_reasons.append("valid_sample_quality_below_threshold")
    if quality.get("return_unit") != "percent_points":
        quality_block_reasons.append("expectancy_units_uncertain")
    recommendations_blocked = bool(quality_block_reasons)
    groups = {
        "sector": _group(rows, "sector", "sector"),
        "signal_type": _group(rows, "signal_type", "signal_type"),
        "catalyst_strength": _group(rows, "catalyst_strength", "catalyst_strength"),
        "rvol_bucket": _group(rows, "rvol_bucket", "rvol_bucket"),
        "gain_bucket": _group(rows, "gain_bucket", "gain_bucket"),
        "market_regime": _group(rows, "market_regime", "market_regime"),
    }
    best_route = route_rows[0] if route_rows else None
    worst_route = route_rows[-1] if route_rows else None
    suggested_config = _dynamic_momentum_gate_suggestions(route_rows, symbol_rows)
    if recommendations_blocked:
        suggested_config = {
            "research_only": True,
            "recommendation_status": "BLOCKED_DATA_QUALITY",
            "blocking_reasons": quality_block_reasons,
            "suggested_config": {},
        }
    return {
        "report": "signal_expectancy_report",
        "research_only": True,
        "date": day_s,
        "user_id": str(user_id or "default"),
        "generated_at": datetime.now(_UTC).isoformat(),
        "loader_schema_version": CANONICAL_BAR_LOADER_SCHEMA_VERSION,
        "input_fingerprint": input_fingerprint,
        "source_files": source_files,
        "source_file_stats": source_stats,
        "git_commit": git_commit,
        "executive_summary": {
            "best_route_by_expectancy": best_route,
            "worst_route_by_expectancy": worst_route,
            "best_symbols": symbol_rows[:10],
            "worst_symbols": list(reversed(symbol_rows[-10:])),
            "routes_to_increase": [] if recommendations_blocked else [row for row in route_rows if float(row.get("expectancy_score") or 0.0) > 1.0],
            "routes_to_reduce": [] if recommendations_blocked else [row for row in route_rows if float(row.get("expectancy_score") or 0.0) < -0.5],
            "recommendation_status": "BLOCKED_DATA_QUALITY" if recommendations_blocked else "AVAILABLE",
            "recommendation_blocking_reasons": quality_block_reasons,
        },
        "route_expectancy": route_rows,
        "symbol_expectancy": symbol_rows,
        "time_of_day_expectancy": time_rows,
        "groups": groups,
        "trade_decision_recommendations": _blocked_recommendations(quality_block_reasons) if recommendations_blocked else _recommendations(route_rows, symbol_rows, time_rows),
        "suggested_config": suggested_config,
        "data_quality": {
            **quality,
            "missing_exit_context": sum(1 for row in rows if row.get("traded") and row.get("realized_pnl") is None),
            "missing_attribution_rows": 0 if payload.get("orders") or payload.get("candidates") else len(rows),
            "log_lines_read": len(lines),
            "signals_analyzed": len(rows),
            "exits_loaded": sum(len(v) for v in exits_by_symbol.values()),
        },
        "signals": sorted(rows, key=lambda row: (str(row.get("timestamp") or ""), str(row.get("symbol") or ""), str(row.get("route") or ""))),
    }


def _fmt(value: Any) -> str:
    return "n/a" if value is None else str(value)


def render_signal_expectancy_report(report: Mapping[str, Any]) -> str:
    """Render signal expectancy analytics as Markdown."""
    summary = report.get("executive_summary") if isinstance(report.get("executive_summary"), Mapping) else {}
    best = summary.get("best_route_by_expectancy") if isinstance(summary.get("best_route_by_expectancy"), Mapping) else {}
    worst = summary.get("worst_route_by_expectancy") if isinstance(summary.get("worst_route_by_expectancy"), Mapping) else {}
    lines = [
        f"# Signal Expectancy Report {report.get('date')} user={report.get('user_id')}",
        "",
        "Read-only analytics: no trading behavior, thresholds, orders, exits, stops, or sizing changed.",
        "",
        "## Executive Summary",
        f"- best route by expectancy: {best.get('route', 'none')} score={best.get('expectancy_score')}",
        f"- worst route by expectancy: {worst.get('route', 'none')} score={worst.get('expectancy_score')}",
            f"- routes to increase: {', '.join(row.get('route', '') for row in summary.get('routes_to_increase') or []) or 'none'}",
            f"- routes to reduce: {', '.join(row.get('route', '') for row in summary.get('routes_to_reduce') or []) or 'none'}",
            f"- recommendation status: {summary.get('recommendation_status', 'AVAILABLE')}",
        "",
        "## Route Expectancy",
        "| route | count | trades | skipped | avg_15m_return | avg_30m_return | realized_pnl | win_rate | profit_factor | expectancy_score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.get("route_expectancy") or []:
        lines.append(f"| {row.get('route')} | {row.get('count')} | {row.get('trades')} | {row.get('skipped')} | {_fmt(row.get('avg_15m_return'))} | {_fmt(row.get('avg_30m_return'))} | {_fmt(row.get('realized_pnl'))} | {_fmt(row.get('win_rate'))} | {_fmt(row.get('profit_factor'))} | {_fmt(row.get('expectancy_score'))} |")
    lines.extend(["", "## Symbol Expectancy", "| symbol | route | count | trades | avg_forward_return | realized_pnl | win_rate | MFE | MAE | recommendation |", "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])
    for row in (report.get("symbol_expectancy") or [])[:30]:
        lines.append(f"| {row.get('symbol')} | {row.get('route')} | {row.get('count')} | {row.get('trades')} | {_fmt(row.get('avg_forward_return'))} | {_fmt(row.get('realized_pnl'))} | {_fmt(row.get('win_rate'))} | {_fmt(row.get('max_favorable_excursion'))} | {_fmt(row.get('max_adverse_excursion'))} | {row.get('recommendation')} |")
    lines.extend(["", "## Time-of-Day Expectancy", "| time_bucket | count | trades | avg_15m_return | realized_pnl | expectancy_score |", "| --- | ---: | ---: | ---: | ---: | ---: |"])
    for row in report.get("time_of_day_expectancy") or []:
        lines.append(f"| {row.get('time_bucket')} | {row.get('count')} | {row.get('trades')} | {_fmt(row.get('avg_15m_return'))} | {_fmt(row.get('realized_pnl'))} | {_fmt(row.get('expectancy_score'))} |")
    lines.extend(["", "## Trade Decision Recommendations"])
    for rec in report.get("trade_decision_recommendations") or []:
        lines.append(f"- {rec}")
    suggested = report.get("suggested_config") if isinstance(report.get("suggested_config"), Mapping) else {}
    lines.extend(
        [
            "",
            "## Suggested Config",
            "Research-only recommendation; no config is applied by this report.",
            f"- route: {suggested.get('route', 'dynamic_momentum_override')}",
            f"- route expectancy score: {_fmt(suggested.get('route_expectancy_score'))}",
            f"- route sample count: {suggested.get('route_sample_count', 0)}",
            f"- route-level recommendation: {suggested.get('route_level_recommendation', 'hold_current_gate')}",
            f"- candidate reduced_symbols: {', '.join(suggested.get('candidate_reduced_symbols') or []) or 'none'}",
            f"- candidate blocked_symbols: {', '.join(suggested.get('candidate_blocked_symbols') or []) or 'none'}",
        ]
    )
    dq = report.get("data_quality") if isinstance(report.get("data_quality"), Mapping) else {}
    failure_breakdown = dq.get("lookup_failure_breakdown") if isinstance(dq.get("lookup_failure_breakdown"), Mapping) else {}
    symbols_missing = dq.get("symbols_missing_bars") if isinstance(dq.get("symbols_missing_bars"), Sequence) and not isinstance(dq.get("symbols_missing_bars"), str) else []
    lines.extend(
        [
            "",
            "## Data Quality",
            f"- signals analyzed: {dq.get('signals_analyzed', 0)}",
            f"- signals with valid forward bars: {dq.get('signals_with_valid_forward_bars', 0)}",
            f"- missing bars: {dq.get('missing_bars', 0)}",
            f"- lookup success rate: {_fmt(dq.get('lookup_success_rate'))}",
            f"- lookup failure breakdown: {json.dumps(failure_breakdown, sort_keys=True)}",
            f"- symbols missing bars: {', '.join(str(sym) for sym in symbols_missing[:30]) or 'none'}",
            f"- time buckets missing bars: {json.dumps(dq.get('time_buckets_missing_bars') or {}, sort_keys=True)}",
            f"- earliest available bar: {_fmt(dq.get('earliest_available_bar'))}",
            f"- latest available bar: {_fmt(dq.get('latest_available_bar'))}",
            f"- source selected: {', '.join(dq.get('source_selected') or []) or 'none'}",
            f"- cache hits: {dq.get('cache_hits', 0)}",
            f"- cache misses: {dq.get('cache_misses', 0)}",
            f"- persistence status: {json.dumps(dq.get('persistence_status') or {}, sort_keys=True)}",
            f"- missing entry context: {dq.get('missing_entry_context', 0)}",
            f"- missing exit context: {dq.get('missing_exit_context', 0)}",
            f"- missing attribution rows: {dq.get('missing_attribution_rows', 0)}",
            f"- return unit: {dq.get('return_unit', 'percent_points')}",
            f"- forward horizon valid counts: {json.dumps(dq.get('forward_horizon_valid_counts') or {}, sort_keys=True)}",
            f"- forward horizon missing counts: {json.dumps(dq.get('forward_horizon_missing_counts') or {}, sort_keys=True)}",
            f"- anomalous observations: {dq.get('anomalous_observations', 0)}",
            f"- excluded observations: {dq.get('excluded_observations', 0)}",
            f"- anomaly breakdown: {json.dumps(dq.get('anomaly_breakdown') or {}, sort_keys=True)}",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_signal_expectancy_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    bars_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write signal expectancy artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_signal_expectancy_report(project_root=project_root, data_dir=data, day=day_s, user_id=user_id, bars_dir=bars_dir, log_text=log_text, log_files=log_files)
    out_dir = data / "research_metrics" / day_s
    json_path = out_dir / "signal_expectancy_report.json"
    text_path = out_dir / "signal_expectancy_report.md"
    from src.artifact_writability import atomic_write_text

    atomic_write_text(json_path, json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", generator="signal_expectancy_report")
    atomic_write_text(text_path, render_signal_expectancy_report(report), generator="signal_expectancy_report")
    return json_path, text_path, report


__all__ = ["build_signal_expectancy_report", "render_signal_expectancy_report", "write_signal_expectancy_report"]
