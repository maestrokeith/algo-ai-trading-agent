"""Research-only intraday bar availability and backfill helpers."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time as time_module
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.artifact_writability import atomic_write_text

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
EXPECTED_BAR_DIR_NAMES = ("historical_bars", "bars", "market_bars", "intraday_bars")
REQUIRED_BAR_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
CANONICAL_BAR_LOADER_SCHEMA_VERSION = "canonical_research_bars_v2"
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,12}$")
_BACKFILL_1MIN_RE = re.compile(r"^(?P<symbol>[A-Z][A-Z0-9.\-]{0,12})_(?P<day>\d{4}-\d{2}-\d{2})_1Min\.csv$")
_REPLAY_LIKE_RE = re.compile(r"(^|[_\-\s:])(replay|mock|test|shadow|paper)($|[_\-\s:])")
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


@dataclass(frozen=True)
class ResearchBarsPaths:
    json_path: Path
    text_path: Path


_BAR_LOAD_CACHE: dict[tuple[str, int, int, int, str, str], tuple[pd.DataFrame | None, dict[str, Any]]] = {}


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def expected_bar_dirs(data_dir: Path | str = "data") -> list[Path]:
    """Return local bar roots used by research report readers."""
    root = Path(data_dir)
    return [root / name for name in EXPECTED_BAR_DIR_NAMES]


def clear_bar_loader_cache() -> None:
    """Clear in-process canonical bar loader state."""
    _BAR_LOAD_CACHE.clear()


def canonical_bar_root(data_dir: Path | str = "data") -> Path:
    """Return the canonical persisted research bar root."""
    return Path(data_dir) / "historical_bars"


def canonical_bar_path(data_dir: Path | str, symbol: str, day: str, timeframe: str = "1Min") -> Path:
    """Return the canonical persisted bar path for one symbol/date/timeframe."""
    tf = "1Min" if str(timeframe).lower() in {"1min", "1m", "minute"} else str(timeframe)
    return canonical_bar_root(data_dir) / f"{_normalize_symbol(symbol)}_{day}_{tf}.csv"


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


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
        return dt.replace(tzinfo=_UTC)
    return dt


def _atomic_write_text(path: Path, text: str) -> None:
    atomic_write_text(path, text, generator="research_bars")


def _atomic_write_frame_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    os.close(fd)
    try:
        frame.to_csv(tmp_path, index=False)
        with tmp_path.open("rb") as fh:
            os.fsync(fh.fileno())
        tmp_path.replace(path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _atomic_write_validated_bar_csv(path: Path, frame: pd.DataFrame, *, symbol: str, day: str) -> dict[str, Any]:
    """Write a bar CSV only if the exact bytes can be read by the canonical inspector."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    os.close(fd)
    try:
        frame.to_csv(tmp_path, index=False)
        with tmp_path.open("rb") as fh:
            os.fsync(fh.fileno())
        tmp_inspection = inspect_bar_file(tmp_path, symbol=symbol, day=day)
        if not tmp_inspection.get("usable"):
            return {"written": False, "reason": "post_write_validation_failed", "post_write_validation": tmp_inspection}
        tmp_path.replace(path)
        clear_bar_loader_cache()
        final = inspect_bar_file(path, symbol=symbol, day=day)
        if not final.get("usable"):
            return {"written": False, "reason": "post_write_validation_failed", "post_write_validation": final}
        return {"written": True, "reason": None, "post_write_validation": final}
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _date_from_path(path: Path) -> str | None:
    name = path.name
    for idx in range(max(len(name) - 9, 0)):
        chunk = name[idx : idx + 10]
        if len(chunk) == 10 and chunk[4] == "-" and chunk[7] == "-":
            try:
                datetime.strptime(chunk, "%Y-%m-%d")
            except ValueError:
                continue
            return chunk
    for idx in range(max(len(name) - 7, 0)):
        chunk = name[idx : idx + 8]
        if not chunk.isdigit():
            continue
        try:
            datetime.strptime(chunk, "%Y%m%d")
        except ValueError:
            continue
        return f"{chunk[:4]}-{chunk[4:6]}-{chunk[6:8]}"
    return None


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _iter_mapping_rows(payload: Any, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    rows: list[Mapping[str, Any]] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            rows.extend(row for row in value if isinstance(row, Mapping))
    return rows


def _payload_day(payload: Any, path: Path) -> str | None:
    if isinstance(payload, Mapping):
        ts = _parse_timestamp(payload.get("generated_at") or payload.get("created_at") or payload.get("timestamp"))
        if ts is not None:
            return ts.astimezone(_ET).date().isoformat()
        day = payload.get("date") or payload.get("trading_date")
        if isinstance(day, str) and len(day) >= 10:
            return day[:10]
    return _date_from_path(path)


def infer_research_symbols(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
) -> list[str]:
    """Infer symbols that research reports may need bars for on a date."""
    data_path = Path(data_dir)
    symbols: set[str] = set()
    safe_user = _safe_user(user_id)

    history_root = data_path / "dynamic_scan_history"
    if history_root.exists():
        exact_paths = sorted(history_root.glob(f"*{day.replace('-', '')}*_{safe_user}.json"))
        fallback_paths = sorted(history_root.glob(f"*{day.replace('-', '')}*_default.json"))
        for path in exact_paths or fallback_paths:
            payload = _load_json(path)
            if _payload_day(payload, path) != day:
                continue
            for row in _iter_mapping_rows(payload, ("candidates", "accepted", "rejected", "selected")):
                symbol = str(row.get("symbol") or "").strip().upper()
                if symbol:
                    symbols.add(symbol)

    research_roots = (
        data_path / "research" / "trend_prefilter_research",
        data_path / "research" / "dynamic_gate_research",
        data_path / "research" / "dynamic_rvol_sensitivity",
        data_path / "research" / "catalyst_outcomes",
    )
    for root in research_roots:
        path = root / f"{day}_{safe_user}.json"
        if not path.exists() and safe_user != "default":
            path = root / f"{day}_default.json"
        if not path.exists():
            continue
        payload = _load_json(path)
        for row in _iter_mapping_rows(
            payload,
            (
                "symbols",
                "candidates",
                "rvol_only_examples",
                "accepted",
                "rejected",
                "outcomes",
                "records",
                "events",
            ),
        ):
            symbol = str(row.get("symbol") or "").strip().upper()
            if symbol:
                symbols.add(symbol)
        if isinstance(payload, Mapping):
            key_examples = payload.get("key_symbol_examples")
            if isinstance(key_examples, Mapping):
                symbols.update(str(sym).strip().upper() for sym in key_examples if str(sym).strip())
    return sorted(symbols)


def discover_bar_paths(data_dir: Path | str, symbol: str, day: str, timeframe: str = "1Min") -> list[Path]:
    """Discover canonical and supported legacy bar files for a symbol/date."""
    data_path = Path(data_dir)
    symbol_u = _normalize_symbol(symbol)
    compact = day.replace("-", "")
    canonical = canonical_bar_path(data_path, symbol_u, day, timeframe)
    patterns: list[str] = []
    for suffix in ("csv", "json"):
        patterns.extend(
            [
                f"**/{symbol_u}*{day}*.{suffix}",
                f"**/{day}*{symbol_u}*.{suffix}",
                f"**/{symbol_u}*{compact}*.{suffix}",
                f"**/{compact}*{symbol_u}*.{suffix}",
                f"**/{day}/**/{symbol_u}.{suffix}",
                f"**/{compact}/**/{symbol_u}.{suffix}",
                f"**/{symbol_u}.{suffix}",
            ]
        )
    paths: list[Path] = []
    if canonical.exists():
        paths.append(canonical)
    def path_matches_symbol(path: Path) -> bool:
        stem = path.name.upper()
        compact_name = stem.replace("-", "")
        if stem.startswith(f"{symbol_u}_") or stem == f"{symbol_u}.CSV" or stem == f"{symbol_u}.JSON":
            return True
        if f"/{day}/{symbol_u}." in str(path).upper() or f"/{compact}/{symbol_u}." in str(path).upper():
            return True
        return bool(
            re.search(rf"(^|[^A-Z0-9]){re.escape(symbol_u)}([^A-Z0-9]|$)", stem)
            and (day in stem or compact in compact_name)
        )

    for root in expected_bar_dirs(data_dir):
        if not root.exists():
            continue
        for pattern in patterns:
            paths.extend(path for path in root.glob(pattern) if path.is_file() and path_matches_symbol(path))
    return list(dict.fromkeys(paths))


def _candidate_bar_files(data_dir: Path, symbol: str, day: str) -> list[Path]:
    return discover_bar_paths(data_dir, symbol, day)


def _read_bar_file(path: Path) -> tuple[str, pd.DataFrame | None]:
    try:
        if path.suffix.lower() == ".csv" or ".csv." in path.name.lower():
            return "csv", pd.read_csv(path)
        loaded = _load_json(path)
        rows = loaded.get("bars") if isinstance(loaded, Mapping) else loaded
        return "json", pd.DataFrame(rows)
    except Exception:
        return path.suffix.lower().lstrip(".") or "unknown", None


def _infer_backfill_1min_timestamps(path: Path, frame: pd.DataFrame, day: str) -> pd.Series | None:
    match = _BACKFILL_1MIN_RE.match(path.name)
    if not match or match.group("day") != day:
        return None
    start = pd.Timestamp(f"{day} 09:30:00", tz=_ET)
    return pd.Series(pd.date_range(start=start, periods=len(frame), freq="min").tz_convert(_UTC), index=frame.index)


def _stat_cache_key(path: Path, *, symbol: str, day: str) -> tuple[str, int, int, int, str, str] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size), int(stat.st_ino), _normalize_symbol(symbol), day)


def load_canonical_bar_file(path: Path | str, *, symbol: str, day: str) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """Load one persisted bar file through the authoritative normalizer."""
    p = Path(path)
    key = _stat_cache_key(p, symbol=symbol, day=day)
    if key is not None and key in _BAR_LOAD_CACHE:
        frame, meta = _BAR_LOAD_CACHE[key]
        cached = {**meta, "loader_cache_status": "hit"}
        return (frame.copy() if frame is not None else None), cached
    inspection = inspect_bar_file(p, symbol=symbol, day=day)
    meta: dict[str, Any] = {
        "loader_schema_version": CANONICAL_BAR_LOADER_SCHEMA_VERSION,
        "loader_cache_status": "miss",
        "path": str(p),
        "inspection": inspection,
        "reason": inspection.get("reason"),
        "usable": bool(inspection.get("usable")),
    }
    frame: pd.DataFrame | None = None
    if inspection.get("usable") or inspection.get("reason") == "missing_required_columns":
        _, raw = _read_bar_file(p)
        if raw is not None:
            missing_columns = ((inspection.get("validation") or {}).get("missing_columns") or []) if isinstance(inspection.get("validation"), Mapping) else []
            if not inspection.get("usable") and set(missing_columns) == {"timestamp"}:
                inferred = _infer_backfill_1min_timestamps(p, raw, day)
                if inferred is not None:
                    raw = raw.copy()
                    raw.insert(0, "timestamp", inferred)
            normalized, validation = _normalize_required_bars(raw, day=day, symbol=symbol)
            meta["validation"] = validation
            if not normalized.empty and validation.get("valid"):
                work = normalized.copy()
                work["_timestamp"] = pd.to_datetime(work["timestamp"], utc=True, errors="coerce")
                work = work.loc[work["_timestamp"].notna()].sort_values("_timestamp").drop_duplicates(subset=["_timestamp"], keep="last").reset_index(drop=True)
                frame = work
                meta.update(
                    {
                        "reason": None,
                        "usable": True,
                        "rows": int(len(work)),
                        "first_timestamp": pd.Timestamp(work["_timestamp"].min()).isoformat(),
                        "last_timestamp": pd.Timestamp(work["_timestamp"].max()).isoformat(),
                    }
                )
    if key is not None:
        _BAR_LOAD_CACHE[key] = (frame.copy() if frame is not None else None, {**meta, "loader_cache_status": "miss"})
    return (frame.copy() if frame is not None else None), meta


def load_canonical_bars(
    data_dir: Path | str,
    *,
    symbol: str,
    day: str,
    bars_dir: Path | str | None = None,
) -> tuple[pd.DataFrame | None, dict[str, Any]]:
    """Discover and load usable bars for one symbol/date using canonical semantics."""
    roots = [Path(bars_dir)] if bars_dir is not None else expected_bar_dirs(data_dir)
    paths = discover_bar_paths(Path(data_dir), symbol, day) if bars_dir is None else discover_bar_paths(Path(bars_dir).parent, symbol, day)
    if bars_dir is not None:
        allowed = Path(bars_dir).resolve()
        paths = [path for path in paths if allowed in path.resolve().parents or path.resolve() == allowed]
    frames: list[pd.DataFrame] = []
    inspections: list[dict[str, Any]] = []
    invalid_reasons: Counter[str] = Counter()
    for path in paths:
        frame, meta = load_canonical_bar_file(path, symbol=symbol, day=day)
        inspections.append(meta)
        if frame is None or frame.empty:
            invalid_reasons[str(meta.get("reason") or "invalid_persisted_file")] += 1
            continue
        frames.append(frame)
    if not frames:
        reason = "storage_missing" if not any(root.exists() for root in roots) else "no_historical_source"
        if paths:
            reason = next(iter(invalid_reasons), "invalid_persisted_file")
        return None, {
            "loader_schema_version": CANONICAL_BAR_LOADER_SCHEMA_VERSION,
            "symbol": _normalize_symbol(symbol),
            "day": day,
            "candidate_files": [str(path) for path in paths],
            "file_lookup_attempts": len(paths),
            "file_exists_hits": len(paths),
            "valid_parsed_file_hits": 0,
            "invalid_file_hits": sum(invalid_reasons.values()),
            "missing_file_count": 0 if paths else 1,
            "reason": reason,
            "persistence_status": {
                "storage_missing": "bar_directories_missing",
                "no_historical_source": "no_local_bar_file_for_symbol_day",
                "empty_file": "bar_files_empty",
                "missing_required_columns": "bar_files_missing_required_columns",
                "timestamp_parse_error": "bar_files_invalid_timestamp",
                "invalid_ohlcv": "bar_files_invalid_ohlcv",
                "wrong_symbol": "bar_files_wrong_symbol",
                "wrong_date": "bar_files_wrong_date",
                "unreadable_cache": "bar_files_unreadable_or_empty",
            }.get(reason, "bar_files_corrupted"),
            "file_inspections": inspections,
        }
    bars = pd.concat(frames, ignore_index=True).sort_values("_timestamp").drop_duplicates(subset=["_timestamp"], keep="last")
    return bars.reset_index(drop=True), {
        "loader_schema_version": CANONICAL_BAR_LOADER_SCHEMA_VERSION,
        "symbol": _normalize_symbol(symbol),
        "day": day,
        "candidate_files": [str(path) for path in paths],
        "files_found": [str(path) for path in paths],
        "source_selected": str(paths[0]) if paths else None,
        "file_lookup_attempts": len(paths),
        "file_exists_hits": len(paths),
        "valid_parsed_file_hits": len(frames),
        "invalid_file_hits": sum(invalid_reasons.values()),
        "missing_file_count": 0,
        "reason": None,
        "persistence_status": "loaded",
        "rows": int(len(bars)),
        "first_timestamp": pd.Timestamp(bars["_timestamp"].min()).isoformat(),
        "last_timestamp": pd.Timestamp(bars["_timestamp"].max()).isoformat(),
        "file_inspections": inspections,
    }


def _bar_timestamps(frame: pd.DataFrame | None) -> pd.Series | None:
    if frame is None or frame.empty:
        return None
    if isinstance(frame.index, pd.DatetimeIndex):
        parsed = pd.Series(frame.index, index=frame.index)
    else:
        parsed = None
        for col in ("timestamp", "datetime", "time", "ts", "t", "date"):
            if col in frame.columns:
                parsed = pd.to_datetime(frame[col], utc=True, errors="coerce")
                break
        if parsed is None:
            return None
    parsed = pd.to_datetime(parsed, utc=True, errors="coerce")
    return parsed if parsed.notna().any() else None


def _is_market_holiday(day: str) -> bool:
    return day in _MARKET_HOLIDAYS


def _market_day_status(day: str) -> str:
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return "invalid_date"
    if parsed.weekday() >= 5:
        return "weekend"
    if _is_market_holiday(day):
        return "market_holiday"
    return "open"


def _bar_output_path(data_dir: Path, symbol: str, day: str) -> Path:
    return canonical_bar_path(data_dir, symbol, day)


def _column_lookup(columns: Sequence[Any]) -> dict[str, Any]:
    return {str(col).strip().lower(): col for col in columns}


def _extract_symbol_from_frame(frame: pd.DataFrame, *, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return only rows for symbol when the provider response contains multiple symbols."""
    target = _normalize_symbol(symbol)
    if frame is None:
        return pd.DataFrame(), {"symbol_extraction_status": "empty_provider_response"}
    work = frame.copy()
    response_symbols: set[str] = set()
    multiindex_names: list[str] = []
    symbol_level: int | None = None
    timestamp_level: int | None = None
    if isinstance(work.index, pd.MultiIndex):
        multiindex_names = [str(name or "") for name in work.index.names]
        lower_names = [name.lower() for name in multiindex_names]
        for i, name in enumerate(lower_names):
            values = work.index.get_level_values(i)
            parsed = pd.to_datetime(values, utc=True, errors="coerce")
            if name in {"timestamp", "time", "datetime", "date"} or parsed.notna().sum() >= max(1, len(values) // 2):
                timestamp_level = i if timestamp_level is None else timestamp_level
            as_symbols = {_normalize_symbol(value) for value in values if _valid_symbol(_normalize_symbol(value))}
            if name in {"symbol", "ticker"} or target in as_symbols or (as_symbols and parsed.notna().sum() == 0):
                symbol_level = i if symbol_level is None else symbol_level
                response_symbols.update(as_symbols)
        if symbol_level is not None:
            mask = [_normalize_symbol(value) == target for value in work.index.get_level_values(symbol_level)]
            if not any(mask):
                return pd.DataFrame(), {
                    "symbol_extraction_status": "symbol_missing_from_response",
                    "provider_response_symbols": sorted(response_symbols),
                    "multiindex_level_names": multiindex_names,
                    "multiindex_symbol_level": symbol_level,
                    "multiindex_timestamp_level": timestamp_level,
                }
            work = work.loc[mask].copy()
    lookup = _column_lookup(work.columns)
    symbol_col = next((lookup[name] for name in ("symbol", "ticker") if name in lookup), None)
    if symbol_col is not None:
        values = work[symbol_col].map(_normalize_symbol)
        present = sorted(set(value for value in values if value))
        response_symbols.update(present)
        if present and target not in present:
            return pd.DataFrame(), {"symbol_extraction_status": "wrong_symbol", "provider_response_symbols": sorted(response_symbols), "symbol_column": str(symbol_col)}
        work = work.loc[values == target].copy()
        if work.empty:
            return pd.DataFrame(), {"symbol_extraction_status": "empty_symbol_slice", "provider_response_symbols": sorted(response_symbols), "symbol_column": str(symbol_col)}
    return work, {
        "symbol_extraction_status": "single_symbol" if not response_symbols else "matched",
        "provider_response_symbols": sorted(response_symbols),
        "multiindex_level_names": multiindex_names,
        "multiindex_symbol_level": symbol_level,
        "multiindex_timestamp_level": timestamp_level,
    }


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _valid_symbol(symbol: str) -> bool:
    return bool(symbol and _SYMBOL_RE.match(symbol))


def _is_replay_like_row(row: Mapping[str, Any]) -> bool:
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
            "decision_id",
            "logical_order_id",
            "client_order_id",
            "broker_order_id",
            "broker_fill_id",
            "position_id",
            "order_id",
            "fill_id",
        )
    ).lower()
    return any(prefix in text for prefix in ("replay-", "mock-", "test-", "shadow-", "paper-")) or bool(_REPLAY_LIKE_RE.search(text))


def _normalize_required_bars(frame: pd.DataFrame, *, day: str, symbol: str | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    extraction: dict[str, Any] = {}
    source = frame
    if symbol:
        source, extraction = _extract_symbol_from_frame(frame, symbol=symbol)
        if source.empty and extraction.get("symbol_extraction_status") in {"symbol_missing_from_response", "wrong_symbol", "empty_symbol_slice"}:
            return pd.DataFrame(), {"valid": False, "reason": extraction["symbol_extraction_status"], "rows": 0, **extraction}
    normalized = _normalize_frame_for_write(source)
    if normalized.empty:
        return pd.DataFrame(), {"valid": False, "reason": "empty_provider_response", "rows": 0, **extraction}
    missing_columns = [col for col in REQUIRED_BAR_COLUMNS if col not in normalized.columns]
    if missing_columns:
        return pd.DataFrame(), {
            "valid": False,
            "reason": "missing_required_columns",
            "missing_columns": missing_columns,
            "rows": int(len(normalized)),
            "columns": [str(col) for col in normalized.columns],
            **extraction,
        }
    work = normalized.copy()
    timestamps = pd.to_datetime(work["timestamp"], utc=True, errors="coerce")
    work = work.loc[timestamps.notna()].copy()
    if work.empty:
        return pd.DataFrame(), {"valid": False, "reason": "timestamp_parse_error", "rows": 0, **extraction}
    work["timestamp"] = timestamps.loc[timestamps.notna()].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    for col in ("open", "high", "low", "close", "volume"):
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["open", "high", "low", "close", "volume"])
    if work.empty:
        return pd.DataFrame(), {"valid": False, "reason": "invalid_ohlcv", **extraction}
    ts = pd.to_datetime(work["timestamp"], utc=True, errors="coerce")
    mask = ts.dt.tz_convert(_ET).dt.date.astype(str) == day
    work = work.loc[mask].copy()
    if work.empty:
        return pd.DataFrame(), {"valid": False, "reason": "wrong_date", **extraction}
    work["_sort_ts"] = pd.to_datetime(work["timestamp"], utc=True, errors="coerce")
    work = work.sort_values("_sort_ts").drop_duplicates(subset=["_sort_ts"], keep="last")
    first = work["_sort_ts"].min()
    last = work["_sort_ts"].max()
    optional = [col for col in ("trade_count", "vwap", "symbol") if col in work.columns]
    work = work[list(REQUIRED_BAR_COLUMNS) + optional].reset_index(drop=True)
    return work, {
        "valid": True,
        "rows": int(len(work)),
        "first_timestamp": pd.Timestamp(first).isoformat(),
        "last_timestamp": pd.Timestamp(last).isoformat(),
        **extraction,
    }


def inspect_bar_file(path: Path | str, *, symbol: str, day: str) -> dict[str, Any]:
    """Inspect a persisted bar CSV/JSON and classify whether expectancy can use it."""
    p = Path(path)
    result: dict[str, Any] = {
        "symbol": _normalize_symbol(symbol),
        "path": str(p),
        "file_size": None,
        "raw_line_count": None,
        "parsed_row_count": 0,
        "header": None,
        "observed_columns": [],
        "index_representation": None,
        "first_data_row": None,
        "last_data_row": None,
        "timestamp_parse_result": "unavailable",
        "earliest_timestamp": None,
        "latest_timestamp": None,
        "duplicate_timestamp_count": 0,
        "null_timestamp_count": 0,
        "null_ohlcv_count": 0,
        "invalid_numeric_ohlcv_count": 0,
        "classification": "unreadable",
        "reason": "unreadable_cache",
        "usable": False,
        "complete": False,
        "partial": False,
    }
    if not p.exists():
        result.update({"classification": "missing", "reason": "no_historical_source"})
        return result
    try:
        result["file_size"] = p.stat().st_size
    except OSError as exc:
        result.update({"reason": "unreadable_cache", "error": f"{type(exc).__name__}: {exc}"})
        return result
    if result["file_size"] == 0:
        result.update({"classification": "zero-byte", "reason": "empty_file"})
        return result
    try:
        text = p.read_text(encoding="utf-8", errors="replace") if p.suffix.lower() == ".csv" else ""
        lines = text.splitlines()
        result["raw_line_count"] = len(lines)
        result["header"] = lines[0] if lines else None
        result["first_data_row"] = lines[1] if len(lines) > 1 else None
        result["last_data_row"] = lines[-1] if len(lines) > 1 else None
    except OSError:
        pass
    fmt, frame = _read_bar_file(p)
    result["format"] = fmt
    if frame is None:
        result.update({"classification": "unreadable", "reason": "unreadable_cache"})
        return result
    result["parsed_row_count"] = int(len(frame))
    result["observed_columns"] = [str(col) for col in frame.columns]
    result["index_representation"] = type(frame.index).__name__
    if frame.empty:
        classification = "header-only" if result.get("raw_line_count") == 1 else "empty"
        result.update({"classification": classification, "reason": "empty_file"})
        return result
    normalized, validation = _normalize_required_bars(frame, day=day, symbol=symbol)
    result["validation"] = validation
    if normalized.empty or not validation.get("valid"):
        reason = str(validation.get("reason") or "corrupted_file")
        result.update({"classification": reason.replace("_", "-"), "reason": reason})
        return result
    ts = pd.to_datetime(normalized["timestamp"], utc=True, errors="coerce")
    result["timestamp_parse_result"] = "ok" if ts.notna().all() else "partial"
    result["null_timestamp_count"] = int(ts.isna().sum())
    result["duplicate_timestamp_count"] = int(ts.duplicated().sum())
    result["earliest_timestamp"] = pd.Timestamp(ts.min()).isoformat()
    result["latest_timestamp"] = pd.Timestamp(ts.max()).isoformat()
    for col in ("open", "high", "low", "close", "volume"):
        numeric = pd.to_numeric(normalized[col], errors="coerce")
        result["null_ohlcv_count"] += int(normalized[col].isna().sum())
        result["invalid_numeric_ohlcv_count"] += int(numeric.isna().sum())
    sorted_ok = bool(ts.is_monotonic_increasing)
    required_ok = all(col in normalized.columns for col in REQUIRED_BAR_COLUMNS)
    ohlcv_ok = result["invalid_numeric_ohlcv_count"] == 0
    symbol_ok = True
    if "symbol" in normalized.columns:
        present = {_normalize_symbol(value) for value in normalized["symbol"] if _normalize_symbol(value)}
        symbol_ok = not present or present == {_normalize_symbol(symbol)}
    if not sorted_ok:
        result.update({"classification": "malformed", "reason": "timestamps_not_sorted"})
        return result
    if not required_ok:
        result.update({"classification": "missing required columns", "reason": "missing_required_columns"})
        return result
    if not ohlcv_ok:
        result.update({"classification": "malformed", "reason": "invalid_ohlcv"})
        return result
    if not symbol_ok:
        result.update({"classification": "wrong-symbol", "reason": "wrong_symbol"})
        return result
    start, end = _session_bounds(day)
    complete = bool(ts.min() <= pd.Timestamp(start) + pd.Timedelta(minutes=5) and ts.max() >= pd.Timestamp(end) - pd.Timedelta(minutes=1))
    result.update(
        {
            "classification": "complete" if complete else "partial",
            "reason": None if complete else "extended_session_incomplete",
            "usable": True,
            "complete": complete,
            "partial": not complete,
            "canonical_row_count": int(len(normalized)),
            "canonical_columns": [str(col) for col in normalized.columns],
        }
    )
    return result


def inspect_forward_bar_cache(*, data_dir: Path | str, symbol: str, day: str) -> dict[str, Any]:
    data_path = Path(data_dir)
    symbol_u = _normalize_symbol(symbol)
    path = canonical_bar_path(data_path, symbol_u, day)
    frame, meta = load_canonical_bars(data_path, symbol=symbol_u, day=day)
    inspection = inspect_bar_file(path, symbol=symbol_u, day=day)
    if not path.exists():
        return {"symbol": symbol_u, "path": str(path), "cache_status": "miss", "complete": False, "reason": meta.get("reason") or "missing_file", "inspection": inspection, "loader": meta}
    if frame is None or frame.empty:
        return {
            "symbol": symbol_u,
            "path": str(path),
            "cache_status": "corrupted",
            "complete": False,
            "reason": meta.get("reason") or inspection.get("reason") or "corrupted_file",
            "inspection": inspection,
            "loader": meta,
        }
    complete = bool(inspection.get("complete"))
    return {
        "symbol": symbol_u,
        "path": str(path),
        "cache_status": "hit" if complete else "partial",
        "complete": complete,
        "reason": None if complete else "incomplete_session",
        "rows": int(meta.get("rows") or inspection.get("canonical_row_count") or inspection.get("parsed_row_count") or 0),
        "first_timestamp": meta.get("first_timestamp") or inspection.get("earliest_timestamp"),
        "last_timestamp": meta.get("last_timestamp") or inspection.get("latest_timestamp"),
        "inspection": inspection,
        "loader": meta,
    }


def inspect_symbol_bars(*, data_dir: Path | str, symbol: str, day: str) -> dict[str, Any]:
    """Inspect local bar files for one symbol/date."""
    data_path = Path(data_dir)
    paths = discover_bar_paths(data_path, symbol, day)
    formats: set[str] = set()
    loaded_frame, meta = load_canonical_bars(data_path, symbol=symbol, day=day)
    rows = int(meta.get("rows") or 0)
    matched_paths = [str(path) for path in paths]
    for path in paths:
        fmt, _raw_frame = _read_bar_file(path)
        formats.add(fmt)
    latest = meta.get("last_timestamp")
    classification = "missing"
    if paths:
        usable = [item for item in meta.get("file_inspections", []) if item.get("usable")]
        classification = "valid_partial" if usable and any((item.get("inspection") or {}).get("partial") for item in usable) else "valid_complete" if usable else str(meta.get("reason") or "invalid")
    return {
        "symbol": symbol,
        "has_bars": loaded_frame is not None and not loaded_frame.empty,
        "valid": loaded_frame is not None and not loaded_frame.empty,
        "partial": classification == "valid_partial",
        "invalid": bool(paths) and not (loaded_frame is not None and not loaded_frame.empty),
        "reason": meta.get("reason"),
        "classification": classification,
        "files": matched_paths,
        "formats": sorted(formats),
        "rows": rows,
        "latest_timestamp": pd.Timestamp(latest).tz_convert(_ET).isoformat() if latest is not None else None,
        "loader": meta,
    }


def build_research_bars_status(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Build an offline diagnostic of local research bar availability."""
    data_path = Path(data_dir)
    requested = [str(sym).strip().upper() for sym in (symbols or []) if str(sym).strip()]
    discovery = discover_forward_bar_symbols(project_root=Path("."), data_dir=data_path, day=day, user_id=user_id, symbols=requested or None)
    canonical_signal_symbols = list(discovery.get("symbols") or [])
    fallback_inferred = infer_research_symbols(data_dir=data_path, day=day, user_id=user_id) if not canonical_signal_symbols else []
    checked_symbols = sorted(dict.fromkeys(requested or canonical_signal_symbols or fallback_inferred))
    dirs = expected_bar_dirs(data_path)
    symbol_rows = [inspect_symbol_bars(data_dir=data_path, symbol=symbol, day=day) for symbol in checked_symbols]
    with_bars = [row["symbol"] for row in symbol_rows if row["has_bars"]]
    partial = [row["symbol"] for row in symbol_rows if row.get("partial")]
    invalid = [row["symbol"] for row in symbol_rows if row.get("invalid")]
    missing = [row["symbol"] for row in symbol_rows if not row["has_bars"] and not row.get("invalid")]
    discovered_files = sorted({path for row in symbol_rows for path in (row.get("files") or [])})
    expected_file_names = {canonical_bar_path(data_path, symbol, day).name for symbol in checked_symbols}
    unexpected = sorted(str(path) for path in canonical_bar_root(data_path).glob(f"*_{day}_1Min.csv") if path.name not in expected_file_names) if canonical_bar_root(data_path).exists() else []
    symbol_source = "explicit" if requested else str(discovery.get("source") or "") if canonical_signal_symbols else "inferred"
    if symbol_source == "research_artifact_inference":
        symbol_source = "inferred"
    return {
        "report": "research_bars_status",
        "research_only": True,
        "date": day,
        "user": user_id,
        "loader_schema_version": CANONICAL_BAR_LOADER_SCHEMA_VERSION,
        "expected_directories": [str(path) for path in dirs],
        "existing_directories": [str(path) for path in dirs if path.exists()],
        "symbol_source": symbol_source,
        "canonical_signal_symbols": canonical_signal_symbols,
        "explicitly_requested_symbols": requested,
        "fallback_inferred_symbols": fallback_inferred,
        "symbols_checked": checked_symbols,
        "symbols_with_bars": with_bars,
        "valid_symbols": with_bars,
        "partial_symbols": partial,
        "invalid_symbols": invalid,
        "symbols_missing_bars": missing,
        "missing_symbols": missing,
        "files_discovered": discovered_files,
        "unexpected_extra_files": unexpected,
        "symbols": symbol_rows,
        "summary": {
            "expected_directories": len(dirs),
            "existing_directories": len([path for path in dirs if path.exists()]),
            "symbols_checked": len(checked_symbols),
            "symbols_with_bars": len(with_bars),
            "valid_symbols": len(with_bars),
            "partial_symbols": len(partial),
            "invalid_symbols": len(invalid),
            "symbols_missing_bars": len(missing),
            "files_discovered": len(discovered_files),
            "unexpected_extra_files": len(unexpected),
            "total_rows": sum(int(row.get("rows") or 0) for row in symbol_rows),
        },
    }


def render_research_bars_status(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Research Bars Status - {report.get('date')} user={report.get('user')}",
        "Research-only: no trading behavior, orders, risk, sizing, allocator, entry, exit, or options logic changed.",
        "",
        "Directories",
    ]
    existing = set(report.get("existing_directories") or [])
    for path in report.get("expected_directories") or []:
        lines.append(f"- {path}: {'exists' if path in existing else 'missing'}")
    lines.extend(
        [
            "",
            "Summary",
            f"- symbol_source: {report.get('symbol_source')}",
            f"- symbols checked: {summary.get('symbols_checked', 0)}",
            f"- symbols with bars: {summary.get('symbols_with_bars', 0)}",
            f"- symbols missing bars: {summary.get('symbols_missing_bars', 0)}",
            "",
            "Symbols With Bars",
        ]
    )
    rows = report.get("symbols") if isinstance(report.get("symbols"), list) else []
    present = [row for row in rows if row.get("has_bars")]
    missing = [row for row in rows if not row.get("has_bars")]
    if not present:
        lines.append("- none")
    for row in present[:100]:
        lines.append(
            f"- {row.get('symbol')} latest={row.get('latest_timestamp')} "
            f"formats={','.join(row.get('formats') or [])} rows={row.get('rows')} files={len(row.get('files') or [])}"
        )
    lines.append("")
    lines.append("Symbols Missing Bars")
    if not missing:
        lines.append("- none")
    else:
        lines.append("- " + ", ".join(str(row.get("symbol")) for row in missing[:200]))
    return "\n".join(lines) + "\n"


def write_research_bars_status(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    data_path = Path(data_dir)
    report = build_research_bars_status(data_dir=data_path, day=day, user_id=user_id, symbols=symbols)
    out_dir = data_path / "research" / "bars_status"
    stem = f"{day}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    _atomic_write_text(json_path, json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    _atomic_write_text(text_path, render_research_bars_status(report))
    return json_path, text_path, report


def build_research_bars_consistency(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compare canonical bar status and signal expectancy availability."""
    from src.signal_expectancy_report import build_signal_expectancy_report

    status = build_research_bars_status(data_dir=data_dir, day=day, user_id=user_id, symbols=symbols)
    expectancy = build_signal_expectancy_report(project_root=project_root, data_dir=data_dir, day=day, user_id=user_id)
    dq = expectancy.get("data_quality") if isinstance(expectancy.get("data_quality"), Mapping) else {}
    lifecycle_expected = set(status.get("symbols_checked") or [])
    backfilled_valid = set(status.get("valid_symbols") or status.get("symbols_with_bars") or [])
    invalid = set(status.get("invalid_symbols") or [])
    inventory_missing = set(status.get("missing_symbols") or status.get("symbols_missing_bars") or [])
    signals = expectancy.get("signals") if isinstance(expectancy.get("signals"), list) else []
    expectancy_required = {_normalize_symbol(row.get("symbol")) for row in signals if isinstance(row, Mapping) and _normalize_symbol(row.get("symbol"))}
    expectancy_used = {
        _normalize_symbol(row.get("symbol"))
        for row in signals
        if isinstance(row, Mapping) and row.get("forward_lookup_status") == "available" and _normalize_symbol(row.get("symbol"))
    }
    expectancy_missing = set(dq.get("symbols_missing_bars") or [])
    missing_required = sorted((expectancy_required - expectancy_used) | (expectancy_missing - expectancy_used))
    unused_available = sorted(backfilled_valid - expectancy_required)
    invalid_required = sorted(invalid & expectancy_required)
    discrepancies: list[dict[str, Any]] = []
    if lifecycle_expected != backfilled_valid | invalid | inventory_missing:
        discrepancies.append({"type": "status_symbol_partition_mismatch", "expected": sorted(lifecycle_expected), "partition": sorted(backfilled_valid | invalid | inventory_missing)})
    if missing_required:
        discrepancies.append({"type": "missing_required_expectancy_symbols", "symbols": missing_required})
    if invalid_required:
        discrepancies.append({"type": "invalid_required_expectancy_symbols", "symbols": invalid_required})
    if int(dq.get("signal_forward_lookup_failures") or 0) > 0:
        discrepancies.append({"type": "signal_forward_window_failures", "count": int(dq.get("signal_forward_lookup_failures") or 0), "breakdown": dq.get("lookup_failure_breakdown") or {}})
    return {
        "report": "research_bars_consistency",
        "research_only": True,
        "date": day,
        "user": user_id,
        "loader_schema_version": CANONICAL_BAR_LOADER_SCHEMA_VERSION,
        "consistent": not discrepancies,
        "discrepancies": discrepancies,
        "lifecycle_expected_symbols": sorted(lifecycle_expected),
        "successfully_backfilled_symbols": sorted(backfilled_valid),
        "expectancy_required_symbols": sorted(expectancy_required),
        "expectancy_used_symbols": sorted(expectancy_used),
        "missing_required_symbols": missing_required,
        "unused_but_available_symbols": unused_available,
        "symbols_expected": sorted(expectancy_required),
        "symbols_valid": sorted(expectancy_used),
        "symbols_invalid": sorted(invalid),
        "symbols_missing": missing_required,
        "unexpected_files": status.get("unexpected_extra_files") or [],
        "row_totals": {
            "status": (status.get("summary") or {}).get("total_rows"),
            "expectancy_files": dq.get("valid_parsed_file_hits"),
        },
        "path_mismatches": [item for item in discrepancies if str(item.get("type")) == "valid_symbol_mismatch"],
        "schema_mismatches": [] if expectancy.get("loader_schema_version") == CANONICAL_BAR_LOADER_SCHEMA_VERSION else [expectancy.get("loader_schema_version")],
        "stale_artifacts": [],
        "cache_discrepancies": [item for item in discrepancies if "count" in str(item.get("type"))],
        "research_bars_status": status,
        "signal_expectancy_data_quality": dq,
    }


def render_research_bars_consistency(report: Mapping[str, Any]) -> str:
    lines = [
        f"Research Bars Consistency - {report.get('date')} user={report.get('user')}",
        "Read-only: no trading behavior, orders, positions, fills, thresholds, sizing, or exits changed.",
        f"- consistent: {bool(report.get('consistent'))}",
        f"- lifecycle expected symbols: {len(report.get('lifecycle_expected_symbols') or [])}",
        f"- successfully backfilled symbols: {len(report.get('successfully_backfilled_symbols') or [])}",
        f"- expectancy required symbols: {len(report.get('expectancy_required_symbols') or [])}",
        f"- expectancy used symbols: {len(report.get('expectancy_used_symbols') or [])}",
        f"- missing required symbols: {len(report.get('missing_required_symbols') or [])}",
        f"- unused but available symbols: {len(report.get('unused_but_available_symbols') or [])}",
        f"- symbols expected: {len(report.get('symbols_expected') or [])}",
        f"- symbols valid: {len(report.get('symbols_valid') or [])}",
        f"- symbols invalid: {len(report.get('symbols_invalid') or [])}",
        f"- symbols missing: {len(report.get('symbols_missing') or [])}",
        f"- unexpected files: {len(report.get('unexpected_files') or [])}",
        "",
        "Discrepancies",
    ]
    discrepancies = report.get("discrepancies") if isinstance(report.get("discrepancies"), list) else []
    if not discrepancies:
        lines.append("- none")
    for item in discrepancies:
        lines.append(f"- {item.get('type')}: {json.dumps(item, sort_keys=True, default=str)}")
    return "\n".join(lines) + "\n"


def write_research_bars_consistency(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    data_path = Path(data_dir)
    report = build_research_bars_consistency(project_root=project_root, data_dir=data_path, day=day, user_id=user_id, symbols=symbols)
    out_dir = data_path / "research" / "bars_consistency"
    stem = f"{day}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    _atomic_write_text(json_path, json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    _atomic_write_text(text_path, render_research_bars_consistency(report))
    return json_path, text_path, report


def _session_bounds(day: str) -> tuple[datetime, datetime]:
    dt = datetime.strptime(day, "%Y-%m-%d").date()
    start = datetime.combine(dt, time(4, 0), tzinfo=_ET).astimezone(_UTC)
    end = datetime.combine(dt, time(20, 0), tzinfo=_ET).astimezone(_UTC)
    return start, end


def _normalize_frame_for_write(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.copy()
    if isinstance(work.index, pd.DatetimeIndex):
        work.insert(0, "timestamp", pd.to_datetime(work.index, utc=True))
    elif isinstance(work.index, pd.MultiIndex):
        idx_names = [str(name or "").lower() for name in work.index.names]
        ts_level = next((i for i, name in enumerate(idx_names) if name in {"timestamp", "time", "datetime", "date"}), None)
        if ts_level is None:
            for i in range(work.index.nlevels):
                parsed = pd.to_datetime(work.index.get_level_values(i), utc=True, errors="coerce")
                if parsed.notna().any():
                    ts_level = i
                    break
        if ts_level is not None:
            work.insert(0, "timestamp", pd.to_datetime(work.index.get_level_values(ts_level), utc=True, errors="coerce"))
    elif "timestamp" not in work.columns:
        for col in ("datetime", "time", "ts", "t", "date"):
            if col in work.columns:
                work = work.rename(columns={col: "timestamp"})
                break
    renames = {
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "open_price": "open",
        "high_price": "high",
        "low_price": "low",
        "close_price": "close",
        "t": "timestamp",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "n": "trade_count",
        "vw": "vwap",
    }
    work = work.rename(columns=renames)
    cols = [col for col in ("timestamp", "open", "high", "low", "close", "volume", "trade_count", "vwap", "symbol") if col in work.columns]
    return work[cols].copy()


def backfill_research_bars(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str],
    broker_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Backfill local 1Min bars for research reports using a supplied or Alpaca broker."""
    data_path = Path(data_dir)
    syms = sorted(dict.fromkeys(str(sym).strip().upper() for sym in symbols if str(sym).strip()))
    if not syms:
        raise ValueError("At least one symbol is required for research bar backfill.")
    start, end = _session_bounds(day)
    if broker_factory is None:
        from src.brokers.alpaca_client import AlpacaBroker
        from src.config_loader import load_config

        config = load_config(Path("config") / "default.yaml")
        broker_factory = lambda: AlpacaBroker(config, paper=str(user_id).strip() != "live_bot")
    broker = broker_factory()
    out_dir = canonical_bar_root(data_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    missing: list[str] = []
    for symbol in syms:
        get_bars = getattr(broker, "get_bars")
        frame = get_bars(symbol, timeframe="1Min", start=start, end=end, limit=2000)
        if frame is None or getattr(frame, "empty", True):
            missing.append(symbol)
            continue
        normalized = _normalize_frame_for_write(frame)
        if normalized.empty:
            missing.append(symbol)
            continue
        path = canonical_bar_path(data_path, symbol, day)
        write_result = _atomic_write_validated_bar_csv(path, normalized, symbol=symbol, day=day)
        if not write_result.get("written"):
            missing.append(symbol)
            continue
        clear_bar_loader_cache()
        written.append({"symbol": symbol, "path": str(path), "rows": int(len(normalized)), "post_write_validation": write_result.get("post_write_validation")})
    return {
        "report": "research_bars_backfill",
        "research_only": True,
        "date": day,
        "user": user_id,
        "timeframe": "1Min",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "output_directory": str(out_dir),
        "written": written,
        "missing": missing,
        "summary": {"requested": len(syms), "written": len(written), "missing": len(missing)},
    }


def discover_forward_bar_symbols(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str] | None = None,
    mode: str | None = "live",
) -> dict[str, Any]:
    """Discover live-scope symbols that need forward bars for signal analytics."""
    explicit = sorted(dict.fromkeys(_normalize_symbol(sym) for sym in (symbols or []) if _normalize_symbol(sym)))
    invalid = sorted(sym for sym in explicit if not _valid_symbol(sym))
    if explicit:
        return {
            "source": "explicit",
            "symbols": [sym for sym in explicit if _valid_symbol(sym)],
            "invalid_symbols": invalid,
            "raw_symbol_count": len(explicit),
        }
    signal_report_path = Path(data_dir) / "research_metrics" / day / "signal_expectancy_report.json"
    signal_payload = _load_json(signal_report_path) if signal_report_path.exists() else None
    if isinstance(signal_payload, Mapping):
        dq = signal_payload.get("data_quality") if isinstance(signal_payload.get("data_quality"), Mapping) else {}
        from_quality = {
            _normalize_symbol(sym)
            for key in ("symbols_missing_bars", "missing_symbols", "symbols_with_bars")
            for sym in (dq.get(key) or [])
            if _normalize_symbol(sym)
        }
        from_signals = {
            _normalize_symbol(row.get("symbol"))
            for row in (signal_payload.get("signals") or [])
            if isinstance(row, Mapping) and not _is_replay_like_row(row) and _normalize_symbol(row.get("symbol"))
        }
        report_symbols = sorted(from_quality | from_signals)
        invalid = sorted(sym for sym in report_symbols if not _valid_symbol(sym))
        valid = [sym for sym in report_symbols if _valid_symbol(sym)]
        if valid:
            return {
                "source": "signal_expectancy_report",
                "path": str(signal_report_path),
                "symbols": valid,
                "invalid_symbols": invalid,
                "raw_symbol_count": len(report_symbols),
            }
    try:
        from src.trading_lifecycle import build_canonical_day

        canonical = build_canonical_day(root=Path(project_root), day=day, user_id=user_id, mode=mode)
        rows: list[Mapping[str, Any]] = []
        for key, value in canonical.items():
            if key in {"sources", "scope", "counts", "integrity_status"}:
                continue
            if isinstance(value, list):
                rows.extend(row for row in value if isinstance(row, Mapping) and not _is_replay_like_row(row))
            elif isinstance(value, Mapping):
                nested = value.values()
                rows.extend(row for row in nested if isinstance(row, Mapping) and row.get("symbol") and not _is_replay_like_row(row))
        source = "canonical_lifecycle"
    except Exception:
        rows = []
        source = "canonical_lifecycle_unavailable"
    inferred = {_normalize_symbol(row.get("symbol")) for row in rows if isinstance(row, Mapping)}
    fallback = set(infer_research_symbols(data_dir=data_dir, day=day, user_id=user_id))
    all_symbols = sorted(sym for sym in (inferred | fallback) if sym)
    invalid = sorted(sym for sym in all_symbols if not _valid_symbol(sym))
    return {
        "source": source if inferred else "research_artifact_inference",
        "symbols": [sym for sym in all_symbols if _valid_symbol(sym)],
        "invalid_symbols": invalid,
        "raw_symbol_count": len(all_symbols),
    }


def _provider_name(broker: Any) -> str:
    return str(getattr(broker, "provider_name", "") or getattr(broker, "name", "") or broker.__class__.__name__ or "unknown")


def _provider_feed(broker: Any) -> str | None:
    for attr in ("_feed_name", "feed_name", "data_feed"):
        value = getattr(broker, attr, None)
        if value:
            return str(value)
    feed = getattr(broker, "_feed_enum", None)
    if feed is not None:
        return str(getattr(feed, "value", None) or getattr(feed, "name", None) or feed)
    return None


def _exception_status_code(exc: Exception) -> Any:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if value is not None:
            return value
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def _classify_provider_exception(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if "subscription" in text or "entitlement" in text or ("sip" in text and ("forbidden" in text or "not entitled" in text)):
        return "feed_entitlement_error"
    if "auth" in text or "credential" in text or "api key" in text or "unauthorized" in text or "forbidden" in text or "401" in text or "403" in text:
        return "authorization_error"
    if "rate" in text or "429" in text or "too many" in text:
        return "rate_limited"
    if "invalid" in text or "bad request" in text or "400" in text:
        return "invalid_request"
    if "timeout" in text or "temporar" in text or "connection" in text or "network" in text:
        return "network_error"
    if "503" in text or "502" in text or "unavailable" in text:
        return "provider_unavailable"
    return "provider_error"


def _payload_keys(payload: Any) -> list[str]:
    if isinstance(payload, Mapping):
        return sorted(str(key) for key in payload.keys())[:50]
    return []


def _provider_payload_diagnostics(payload: Any) -> dict[str, Any]:
    diag: dict[str, Any] = {
        "response_type": type(payload).__name__ if payload is not None else "NoneType",
        "payload_keys": _payload_keys(payload),
        "raw_row_count": 0,
    }
    if isinstance(payload, pd.DataFrame):
        diag.update(
            {
                "dataframe_columns": [str(col) for col in payload.columns],
                "dataframe_index_type": type(payload.index).__name__,
                "dataframe_index_names": [str(name) for name in getattr(payload.index, "names", [])],
                "raw_row_count": int(len(payload)),
            }
        )
        if isinstance(payload.index, pd.MultiIndex):
            symbols: set[str] = set()
            for i in range(payload.index.nlevels):
                symbols.update(_normalize_symbol(value) for value in payload.index.get_level_values(i) if _valid_symbol(_normalize_symbol(value)))
            if symbols:
                diag["provider_response_symbols"] = sorted(symbols)
        lookup = _column_lookup(payload.columns)
        symbol_col = lookup.get("symbol") or lookup.get("ticker")
        if symbol_col is not None:
            diag["provider_response_symbols"] = sorted({_normalize_symbol(value) for value in payload[symbol_col] if _normalize_symbol(value)})
    elif hasattr(payload, "df"):
        df = getattr(payload, "df", None)
        diag["barset_df_type"] = type(df).__name__ if df is not None else "NoneType"
        if isinstance(df, pd.DataFrame):
            diag.update(
                {
                    "dataframe_columns": [str(col) for col in df.columns],
                    "dataframe_index_type": type(df.index).__name__,
                    "dataframe_index_names": [str(name) for name in getattr(df.index, "names", [])],
                    "raw_row_count": int(len(df)),
                }
            )
            if isinstance(df.index, pd.MultiIndex):
                symbols: set[str] = set()
                for i in range(df.index.nlevels):
                    symbols.update(_normalize_symbol(value) for value in df.index.get_level_values(i) if _valid_symbol(_normalize_symbol(value)))
                if symbols:
                    diag["provider_response_symbols"] = sorted(symbols)
    elif isinstance(payload, Mapping):
        bars = payload.get("bars")
        if isinstance(bars, Mapping):
            diag["bar_symbols"] = sorted(str(key) for key in bars.keys())[:100]
        candidate_rows = next((value for value in (payload.get("bars"), payload.get("data"), payload.get("items")) if isinstance(value, list)), None)
        diag["raw_row_count"] = len(candidate_rows) if isinstance(candidate_rows, list) else 0
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        diag["raw_row_count"] = len(payload)
    return diag


def _bar_object_to_row(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    row: dict[str, Any] = {}
    for key in ("timestamp", "time", "t", "open", "high", "low", "close", "volume", "o", "h", "l", "c", "v", "trade_count", "n", "vwap", "vw", "symbol"):
        if hasattr(item, key):
            row[key] = getattr(item, key)
    return row


def _coerce_provider_payload_to_frame(payload: Any, *, symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    diag = _provider_payload_diagnostics(payload)
    if payload is None:
        return pd.DataFrame(), {**diag, "coerce_status": "empty", "reason": "empty_provider_response"}
    if isinstance(payload, pd.DataFrame):
        return payload.copy(), {**diag, "coerce_status": "dataframe"}
    if hasattr(payload, "df"):
        df = getattr(payload, "df", None)
        if isinstance(df, pd.DataFrame):
            return df.copy(), {**diag, "coerce_status": "barset_dataframe", "sdk_shape": "BarSet.df"}
    if isinstance(payload, Mapping):
        rows: Any = None
        bars = payload.get("bars")
        if isinstance(bars, Mapping):
            rows = bars.get(symbol) or bars.get(symbol.upper()) or bars.get(symbol.lower())
        elif isinstance(bars, list):
            rows = bars
        elif isinstance(payload.get(symbol), list):
            rows = payload.get(symbol)
        elif isinstance(payload.get(symbol.upper()), list):
            rows = payload.get(symbol.upper())
        elif isinstance(payload.get("data"), list):
            rows = payload.get("data")
        elif isinstance(payload.get("items"), list):
            rows = payload.get("items")
        if isinstance(rows, list):
            return pd.DataFrame([_bar_object_to_row(item) for item in rows]), {**diag, "coerce_status": "mapping_rows"}
        if isinstance(bars, Mapping):
            return pd.DataFrame(), {**diag, "coerce_status": "empty_symbol_slice", "reason": "symbol_missing_from_response"}
        return pd.DataFrame(), {**diag, "coerce_status": "malformed", "reason": "malformed_provider_response"}
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return pd.DataFrame([_bar_object_to_row(item) for item in payload]), {**diag, "coerce_status": "sequence_rows"}
    return pd.DataFrame(), {**diag, "coerce_status": "malformed", "reason": "malformed_provider_response"}


def _fetch_symbol_bars(
    broker: Any,
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    max_attempts: int,
    retry_sleep_seconds: float,
) -> tuple[pd.DataFrame | None, list[dict[str, Any]], str | None, dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    request_diag = {
        "sdk_method": "broker.get_bars",
        "endpoint": "Alpaca stock historical bars via configured broker",
        "timeframe": "1Min",
        "symbol_argument": symbol,
        "selected_feed": _provider_feed(broker),
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "start_before_end": start < end,
    }
    get_bars = getattr(broker, "get_bars", None)
    if get_bars is None:
        return None, [{"attempt": 0, "status": "failed", "reason": "provider_unavailable"}], "provider_unavailable", request_diag
    last_reason: str | None = None
    last_payload_diag: dict[str, Any] = {}
    for attempt in range(1, max(1, max_attempts) + 1):
        started = time_module.perf_counter()
        try:
            payload = get_bars(symbol, timeframe="1Min", start=start, end=end, limit=2000)
        except Exception as exc:
            reason = _classify_provider_exception(exc)
            last_reason = reason
            attempts.append(
                {
                    "attempt": attempt,
                    "status": "failed",
                    "reason": reason,
                    "provider_exception_class": type(exc).__name__,
                    "http_status_code": _exception_status_code(exc),
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": round((time_module.perf_counter() - started) * 1000.0, 3),
                }
            )
            if reason in {"rate_limited", "network_error", "provider_unavailable"} and attempt < max_attempts:
                time_module.sleep(max(0.0, retry_sleep_seconds))
                continue
            return None, attempts, reason, {**request_diag, "provider_exception_class": type(exc).__name__, "http_status_code": _exception_status_code(exc)}
        latency = round((time_module.perf_counter() - started) * 1000.0, 3)
        frame, payload_diag = _coerce_provider_payload_to_frame(payload, symbol=symbol)
        if not frame.empty:
            frame, extraction = _extract_symbol_from_frame(frame, symbol=symbol)
            payload_diag = {**payload_diag, **extraction}
            if frame.empty and payload_diag.get("symbol_extraction_status") in {"symbol_missing_from_response", "wrong_symbol", "empty_symbol_slice"}:
                payload_diag["reason"] = payload_diag["symbol_extraction_status"]
        last_payload_diag = payload_diag
        if frame.empty:
            last_reason = str(payload_diag.get("reason") or "empty_provider_response")
            attempts.append({"attempt": attempt, "status": "empty", "reason": last_reason, "latency_ms": latency, "provider_result": payload_diag})
            return None, attempts, last_reason, {**request_diag, **payload_diag}
        attempts.append({"attempt": attempt, "status": "success", "rows": int(len(frame)), "latency_ms": latency, "provider_result": payload_diag})
        return frame, attempts, None, {**request_diag, **payload_diag}
    return None, attempts, last_reason or "provider_error", {**request_diag, **last_payload_diag}


def backfill_forward_bars(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str] | None = None,
    broker_factory: Callable[[], Any] | None = None,
    max_attempts: int = 3,
    retry_sleep_seconds: float = 1.0,
    rate_limit_sleep_seconds: float = 0.0,
    force: bool = False,
    mode: str | None = "live",
    diagnostic: bool = False,
) -> dict[str, Any]:
    """Backfill local 1Min bars for forward-outcome analytics without trading side effects."""
    data_path = Path(data_dir)
    market_status = _market_day_status(day)
    discovery = discover_forward_bar_symbols(project_root=project_root, data_dir=data_path, day=day, user_id=user_id, symbols=symbols, mode=mode)
    syms = list(discovery["symbols"])
    start, end = _session_bounds(day)
    rows: list[dict[str, Any]] = []
    summary_counts: Counter[str] = Counter()
    repaired_symbols: set[str] = set()
    if market_status != "open":
        for sym in syms:
            rows.append({"symbol": sym, "status": "failed", "reason": market_status, "provider_selected": None, "fetch_attempts": []})
            summary_counts["failed"] += 1
        return {
            "report": "forward_bars_backfill",
            "research_only": True,
            "date": day,
            "user": user_id,
            "timeframe": "1Min",
            "session_timezone": "America/New_York",
            "session_start_utc": start.isoformat(),
            "session_end_utc": end.isoformat(),
            "market_day_status": market_status,
            "diagnostic": bool(diagnostic),
            "symbol_discovery": discovery,
            "provider_selected": None,
            "symbols": rows,
            "summary": {
                "requested": len(syms),
                "successful": 0,
                "skipped": 0,
                "partial": 0,
                "failed": len(syms),
                "invalid_symbols": len(discovery.get("invalid_symbols") or []),
            },
        }
    if not force:
        prechecked: list[dict[str, Any]] = []
        all_cached = bool(syms)
        for symbol in syms:
            path = _bar_output_path(data_path, symbol, day)
            cache = inspect_forward_bar_cache(data_dir=data_path, symbol=symbol, day=day)
            if not cache.get("complete"):
                all_cached = False
                break
            prechecked.append(
                {
                    "symbol": symbol,
                    "status": "skipped",
                    "reason": "existing_complete_cache",
                    "provider_selected": None,
                    "cache_status": cache.get("cache_status"),
                    "persistence_path": str(path),
                    "rows": cache.get("rows"),
                    "first_timestamp": cache.get("first_timestamp"),
                    "last_timestamp": cache.get("last_timestamp"),
                    "fetch_attempts": [],
                }
            )
        if all_cached:
            summary_counts.update(row["status"] for row in prechecked)
            rows_by_symbol = {str(row.get("symbol")): int(row.get("rows") or 0) for row in prechecked if row.get("rows") is not None}
            return {
                "report": "forward_bars_backfill",
                "research_only": True,
                "date": day,
                "user": user_id,
                "timeframe": "1Min",
                "session_timezone": "America/New_York",
                "session_start_utc": start.isoformat(),
                "session_end_utc": end.isoformat(),
                "market_day_status": market_status,
                "diagnostic": bool(diagnostic),
                "symbol_discovery": discovery,
                "provider_selected": None,
                "symbols": prechecked,
                "summary": {
                    "requested": len(syms),
                    "requested_symbols": syms,
                    "successful": 0,
                    "skipped": len(prechecked),
                    "partial": 0,
                    "failed": 0,
                    "invalid_symbols": len(discovery.get("invalid_symbols") or []),
                    "valid_files": len(prechecked),
                    "complete_files": len(prechecked),
                    "partial_files": 0,
                    "failed_files": 0,
                    "rows_by_symbol": rows_by_symbol,
                    "total_persisted_rows": sum(rows_by_symbol.values()),
                    "selected_feed": None,
                    "provider_method": "canonical_cache_skip",
                },
            }
    if broker_factory is None:
        from src.brokers.alpaca_client import AlpacaBroker
        from src.config_loader import load_config

        config = load_config(Path(project_root) / "config" / "default.yaml")
        broker_factory = lambda: AlpacaBroker(config, paper=str(user_id).strip() != "live_bot")
    try:
        broker = broker_factory()
    except Exception as exc:
        reason = _classify_provider_exception(exc)
        provider_error = f"{type(exc).__name__}: {exc}"
        for symbol in syms:
            rows.append(
                {
                    "symbol": symbol,
                    "status": "failed",
                    "reason": reason,
                    "provider_selected": None,
                    "cache_status": inspect_forward_bar_cache(data_dir=data_path, symbol=symbol, day=day).get("cache_status"),
                    "persistence_path": str(_bar_output_path(data_path, symbol, day)),
                    "fetch_attempts": [{"attempt": 0, "status": "failed", "reason": reason, "error": provider_error}],
                }
            )
            summary_counts["failed"] += 1
        return {
            "report": "forward_bars_backfill",
            "research_only": True,
            "date": day,
            "user": user_id,
            "timeframe": "1Min",
            "session_timezone": "America/New_York",
            "session_start_utc": start.isoformat(),
            "session_end_utc": end.isoformat(),
            "market_day_status": market_status,
            "diagnostic": bool(diagnostic),
            "symbol_discovery": discovery,
            "provider_selected": None,
            "provider_error": provider_error,
            "symbols": rows,
            "summary": {
                "requested": len(syms),
                "successful": 0,
                "skipped": 0,
                "partial": 0,
                "failed": int(summary_counts["failed"]),
                "invalid_symbols": len(discovery.get("invalid_symbols") or []),
            },
        }
    provider = _provider_name(broker)
    for symbol in syms:
        if not _valid_symbol(symbol):
            rows.append({"symbol": symbol, "status": "failed", "reason": "unsupported_symbol", "provider_selected": provider, "fetch_attempts": []})
            summary_counts["failed"] += 1
            continue
        path = _bar_output_path(data_path, symbol, day)
        cache = inspect_forward_bar_cache(data_dir=data_path, symbol=symbol, day=day)
        cache_was_corrupted = cache.get("cache_status") == "corrupted"
        if cache.get("complete") and not force:
            rows.append(
                {
                    "symbol": symbol,
                    "status": "skipped",
                    "reason": "existing_complete_cache",
                    "provider_selected": provider,
                    "cache_status": "hit",
                    "persistence_path": str(path),
                    "rows": cache.get("rows"),
                    "first_timestamp": cache.get("first_timestamp"),
                    "last_timestamp": cache.get("last_timestamp"),
                    "fetch_attempts": [],
                }
            )
            summary_counts["skipped"] += 1
            continue
        frame, attempts, failure, provider_result = _fetch_symbol_bars(
            broker,
            symbol,
            start=start,
            end=end,
            max_attempts=max_attempts,
            retry_sleep_seconds=retry_sleep_seconds,
        )
        if frame is None:
            rows.append(
                {
                    "symbol": symbol,
                    "status": "failed",
                    "reason": failure or "provider_error",
                    "provider_selected": provider,
                    "cache_status": cache.get("cache_status"),
                    "persistence_path": str(path),
                    "fetch_attempts": attempts,
                    "provider_result": provider_result,
                }
            )
            summary_counts["failed"] += 1
            if rate_limit_sleep_seconds > 0:
                time_module.sleep(rate_limit_sleep_seconds)
            continue
        normalized, validation = _normalize_required_bars(frame, day=day, symbol=symbol)
        if normalized.empty or not validation.get("valid"):
            reason = str(validation.get("reason") or "malformed_provider_response")
            rows.append(
                {
                    "symbol": symbol,
                    "status": "failed",
                    "reason": reason,
                    "provider_selected": provider,
                    "cache_status": cache.get("cache_status"),
                    "persistence_path": str(path),
                    "fetch_attempts": attempts,
                    "provider_result": provider_result,
                    "validation": validation,
                }
            )
            summary_counts["failed"] += 1
            continue
        write_result = _atomic_write_validated_bar_csv(path, normalized, symbol=symbol, day=day)
        if not write_result.get("written"):
            rows.append(
                {
                    "symbol": symbol,
                    "status": "failed",
                    "reason": write_result.get("reason") or "post_write_validation_failed",
                    "provider_selected": provider,
                    "cache_status": cache.get("cache_status"),
                    "persistence_path": str(path),
                    "fetch_attempts": attempts,
                    "provider_result": provider_result,
                    "validation": validation,
                    "post_write_validation": write_result.get("post_write_validation"),
                }
            )
            summary_counts["failed"] += 1
            continue
        post = inspect_forward_bar_cache(data_dir=data_path, symbol=symbol, day=day)
        if cache_was_corrupted and post.get("cache_status") in {"hit", "partial"}:
            repaired_symbols.add(symbol)
        status = "successful" if post.get("complete") else "partial"
        summary_counts[status] += 1
        rows.append(
            {
                "symbol": symbol,
                "status": status,
                "reason": None if status == "successful" else "incomplete_session",
                "provider_selected": provider,
                "cache_status": cache.get("cache_status"),
                "persistence_path": str(path),
                "rows": int(len(normalized)),
                "first_timestamp": post.get("first_timestamp") or validation.get("first_timestamp"),
                "last_timestamp": post.get("last_timestamp") or validation.get("last_timestamp"),
                "incomplete_session": status == "partial",
                "fetch_attempts": attempts,
                "provider_result": provider_result,
                "validation": validation,
                "post_write_validation": post.get("inspection"),
                "repaired": symbol in repaired_symbols,
            }
        )
        if rate_limit_sleep_seconds > 0:
            time_module.sleep(rate_limit_sleep_seconds)
    rows_by_symbol = {str(row.get("symbol")): int(row.get("rows") or 0) for row in rows if row.get("rows") is not None}
    provider_symbols = sorted(
        {
            str(sym)
            for row in rows
            for sym in ((row.get("provider_result") or {}).get("provider_response_symbols") or (row.get("provider_result") or {}).get("bar_symbols") or [])
        }
    )
    missing_from_response = sorted(
        str(row.get("symbol"))
        for row in rows
        if row.get("reason") in {"symbol_missing_from_response", "empty_symbol_slice"}
    )
    post_failures = [
        {"symbol": row.get("symbol"), "reason": row.get("reason"), "path": row.get("persistence_path")}
        for row in rows
        if row.get("reason") == "post_write_validation_failed"
    ]
    corrupted_files = [
        {"symbol": row.get("symbol"), "reason": ((row.get("post_write_validation") or {}).get("reason") if isinstance(row.get("post_write_validation"), Mapping) else row.get("reason")), "path": row.get("persistence_path")}
        for row in rows
        if row.get("status") == "failed" and row.get("reason") in {"post_write_validation_failed", "unreadable_cache", "corrupted_file", "missing_required_columns", "timestamp_parse_error", "invalid_ohlcv", "wrong_symbol", "wrong_date"}
    ]
    summary = {
        "requested": len(syms),
        "requested_symbols": syms,
        "successful": int(summary_counts["successful"]),
        "skipped": int(summary_counts["skipped"]),
        "partial": int(summary_counts["partial"]),
        "failed": int(summary_counts["failed"]),
        "invalid_symbols": len(discovery.get("invalid_symbols") or []),
        "provider_response_symbols": provider_symbols,
        "symbols_missing_from_response": missing_from_response,
        "valid_files": int(summary_counts["successful"] + summary_counts["partial"] + summary_counts["skipped"]),
        "complete_files": int(summary_counts["successful"] + summary_counts["skipped"]),
        "partial_files": int(summary_counts["partial"]),
        "empty_files": sum(1 for row in rows if row.get("reason") in {"empty_file", "empty_provider_response"}),
        "malformed_files": sum(1 for row in rows if row.get("reason") in {"malformed_provider_response", "timestamp_parse_error", "invalid_ohlcv"}),
        "corrupted_files": len(corrupted_files),
        "repaired_files": len(repaired_symbols),
        "repaired_symbols": sorted(repaired_symbols),
        "skipped_valid_files": int(summary_counts["skipped"]),
        "failed_files": int(summary_counts["failed"]),
        "rows_by_symbol": rows_by_symbol,
        "unreadable_paths": [row.get("persistence_path") for row in rows if row.get("reason") == "unreadable_cache"],
        "empty_symbol_slices": missing_from_response,
        "post_write_validation_failures": post_failures,
        "total_persisted_rows": sum(rows_by_symbol.values()),
        "selected_feed": _provider_feed(broker),
        "provider_method": "broker.get_bars",
    }
    return {
        "report": "forward_bars_backfill",
        "research_only": True,
        "date": day,
        "user": user_id,
        "timeframe": "1Min",
        "session_timezone": "America/New_York",
        "session_start_utc": start.isoformat(),
        "session_end_utc": end.isoformat(),
        "market_day_status": market_status,
        "diagnostic": bool(diagnostic),
        "symbol_discovery": discovery,
        "provider_selected": provider,
        "symbols": rows,
        "summary": summary,
    }


def render_research_bars_backfill(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Research Bars Backfill - {report.get('date')} user={report.get('user')}",
        "Research-only: fetched market data only and wrote local bar CSVs; no trading behavior changed.",
        f"- requested: {summary.get('requested', 0)}",
        f"- written: {summary.get('written', 0)}",
        f"- missing: {summary.get('missing', 0)}",
        f"- output_directory: {report.get('output_directory')}",
        "",
        "Written",
    ]
    for row in report.get("written") or []:
        lines.append(f"- {row.get('symbol')} rows={row.get('rows')} path={row.get('path')}")
    if not report.get("written"):
        lines.append("- none")
    lines.append("")
    lines.append("Missing")
    missing = report.get("missing") or []
    lines.append("- " + ", ".join(missing) if missing else "- none")
    return "\n".join(lines) + "\n"


def render_forward_bars_backfill(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Forward Bars Backfill - {report.get('date')} user={report.get('user')}",
        "Research-only: fetched market data only; no orders, entries, exits, sizing, or trading state changed.",
        f"- provider_selected: {report.get('provider_selected') or 'none'}",
        f"- market_day_status: {report.get('market_day_status')}",
        f"- session_start_utc: {report.get('session_start_utc')}",
        f"- session_end_utc: {report.get('session_end_utc')}",
        f"- requested: {summary.get('requested', 0)}",
        f"- successful: {summary.get('successful', 0)}",
        f"- skipped: {summary.get('skipped', 0)}",
        f"- partial: {summary.get('partial', 0)}",
        f"- failed: {summary.get('failed', 0)}",
        f"- invalid_symbols: {summary.get('invalid_symbols', 0)}",
        f"- valid_files: {summary.get('valid_files', 0)}",
        f"- complete_files: {summary.get('complete_files', 0)}",
        f"- partial_files: {summary.get('partial_files', 0)}",
        f"- corrupted_files: {summary.get('corrupted_files', 0)}",
        f"- repaired_files: {summary.get('repaired_files', 0)}",
        f"- skipped_valid_files: {summary.get('skipped_valid_files', 0)}",
        f"- total_persisted_rows: {summary.get('total_persisted_rows', 0)}",
        f"- selected_feed: {summary.get('selected_feed') or 'n/a'}",
        f"- provider_method: {summary.get('provider_method') or 'n/a'}",
        "",
        "Symbols",
    ]
    for row in report.get("symbols") or []:
        attempts = row.get("fetch_attempts") if isinstance(row.get("fetch_attempts"), list) else []
        provider_result = row.get("provider_result") if isinstance(row.get("provider_result"), Mapping) else {}
        lines.append(
            f"- {row.get('symbol')} status={row.get('status')} reason={row.get('reason') or 'none'} "
            f"rows={row.get('rows', 0)} cache={row.get('cache_status') or 'n/a'} "
            f"first={row.get('first_timestamp') or 'n/a'} last={row.get('last_timestamp') or 'n/a'} "
            f"path={row.get('persistence_path') or 'n/a'} attempts={len(attempts)}"
        )
        if report.get("diagnostic"):
            lines.append(
                f"  provider_result: method={provider_result.get('sdk_method') or 'n/a'} "
                f"feed={provider_result.get('selected_feed') or 'n/a'} "
                f"type={provider_result.get('response_type') or 'n/a'} "
                f"raw_rows={provider_result.get('raw_row_count', 0)} "
                f"index={provider_result.get('dataframe_index_type') or 'n/a'} "
                f"columns={provider_result.get('dataframe_columns') or []}"
            )
    if not report.get("symbols"):
        lines.append("- none")
    invalid = ((report.get("symbol_discovery") or {}).get("invalid_symbols") or []) if isinstance(report.get("symbol_discovery"), Mapping) else []
    lines.extend(["", "Invalid Symbols", "- " + ", ".join(invalid) if invalid else "- none"])
    return "\n".join(lines) + "\n"


def write_forward_bars_backfill(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str,
    symbols: Sequence[str] | None = None,
    broker_factory: Callable[[], Any] | None = None,
    max_attempts: int = 3,
    retry_sleep_seconds: float = 1.0,
    rate_limit_sleep_seconds: float = 0.0,
    force: bool = False,
    mode: str | None = "live",
    diagnostic: bool = False,
) -> tuple[Path, Path, dict[str, Any]]:
    data_path = Path(data_dir)
    report = backfill_forward_bars(
        project_root=project_root,
        data_dir=data_path,
        day=day,
        user_id=user_id,
        symbols=symbols,
        broker_factory=broker_factory,
        max_attempts=max_attempts,
        retry_sleep_seconds=retry_sleep_seconds,
        rate_limit_sleep_seconds=rate_limit_sleep_seconds,
        force=force,
        mode=mode,
        diagnostic=diagnostic,
    )
    out_dir = data_path / "research" / "forward_bars_backfill"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{day}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    _atomic_write_text(json_path, json.dumps(report, indent=2, sort_keys=True, default=str) + "\n")
    _atomic_write_text(text_path, render_forward_bars_backfill(report))
    return json_path, text_path, report


def runtime_forward_bar_capture_enabled(config: Mapping[str, Any] | None) -> bool:
    """Return whether best-effort runtime forward-bar capture is enabled."""
    if not isinstance(config, Mapping):
        return True
    for path in (
        ("research", "forward_bars", "runtime_capture", "enabled"),
        ("market_data", "forward_bars", "runtime_capture", "enabled"),
        ("trading_control", "forward_bar_capture", "enabled"),
    ):
        cur: Any = config
        for key in path:
            if not isinstance(cur, Mapping) or key not in cur:
                cur = None
                break
            cur = cur.get(key)
        if cur is not None:
            return str(cur).strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return True


def capture_runtime_forward_bars(
    *,
    broker: Any,
    data_dir: Path | str,
    user_id: str,
    timestamp: Any,
    symbols: Sequence[str],
    config: Mapping[str, Any] | None = None,
    min_refetch_minutes: float = 15.0,
) -> dict[str, Any]:
    """Best-effort runtime bar capture for future forward-outcome research.

    This helper never places orders and intentionally returns diagnostics instead
    of raising. Runtime callers should treat it as a sidecar.
    """
    if not runtime_forward_bar_capture_enabled(config):
        return {"enabled": False, "reason": "disabled_by_config", "symbols": []}
    ts = _parse_timestamp(timestamp) or datetime.now(tz=_UTC)
    day = ts.astimezone(_ET).date().isoformat()
    if _market_day_status(day) != "open":
        return {"enabled": True, "skipped": True, "reason": _market_day_status(day), "symbols": []}
    data_path = Path(data_dir)
    selected: list[str] = []
    now = datetime.now(tz=_UTC)
    for sym in sorted(dict.fromkeys(_normalize_symbol(symbol) for symbol in symbols if _normalize_symbol(symbol))):
        if not _valid_symbol(sym):
            continue
        cache = inspect_forward_bar_cache(data_dir=data_path, symbol=sym, day=day)
        if cache.get("complete"):
            continue
        path = Path(str(cache.get("path") or _bar_output_path(data_path, sym, day)))
        if path.exists():
            try:
                age_minutes = (now.timestamp() - path.stat().st_mtime) / 60.0
            except OSError:
                age_minutes = min_refetch_minutes
            if age_minutes < min_refetch_minutes:
                continue
        selected.append(sym)
    if not selected:
        return {"enabled": True, "skipped": True, "reason": "cache_recent_or_complete", "symbols": []}
    try:
        return backfill_forward_bars(
            data_dir=data_path,
            day=day,
            user_id=user_id,
            symbols=selected,
            broker_factory=lambda: broker,
            max_attempts=1,
            retry_sleep_seconds=0.0,
            rate_limit_sleep_seconds=0.0,
            mode="live" if str(user_id).strip() == "live_bot" else None,
        )
    except Exception as exc:
        return {"enabled": True, "status": "failed", "reason": "unexpected_exception", "error": f"{type(exc).__name__}: {exc}", "symbols": selected}
