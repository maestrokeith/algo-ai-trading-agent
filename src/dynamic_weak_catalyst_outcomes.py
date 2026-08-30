"""Read-only weak-catalyst dynamic skip outcome research."""

from __future__ import annotations

import ast
import gzip
import json
import math
import re
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.config_loader import load_config
from src.research_bars import _bar_timestamps, _read_bar_file, expected_bar_dirs
from src.trade_attribution import attribution_daily_path

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_REASON = "weak_catalyst_dynamic_non_exceptional_live"
_HORIZONS_MINUTES = (1, 5, 10, 15, 30, 60)
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
_SYSLOG_RE = re.compile(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\b")
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
_PRICE_KEYS = (
    "entry_price",
    "price",
    "paper_current_price",
    "current_price",
    "last_price",
    "ref_price",
    "mid",
    "market_price",
)


@dataclass(frozen=True)
class WeakCatalystOutcomePaths:
    """Artifact paths written for weak-catalyst outcome research."""

    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


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


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(line)}


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
                hh, mm, ss = (int(part) for part in syslog.group("time").split(":"))
                return datetime(expected.year, expected.month, expected.day, hh, mm, ss, tzinfo=_ET)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ET)
    return dt


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _discover_log_paths(project_root: Path, *, data_dir: Path, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    roots = (
        data_dir / "review" / day,
        data_dir / "logs",
        data_dir / "debug_logs",
        project_root / "logs",
        project_root / "reports" / "debug",
    )
    paths: list[Path] = []
    for root in roots:
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


def _journalctl_lines(day: str, *, service: str = "algo.service") -> list[str]:
    try:
        proc = subprocess.run(
            ["journalctl", "-u", service, "--since", f"{day} 00:00:00", "--until", f"{day} 23:59:59", "--no-pager"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def _grep_counts(lines: Sequence[str]) -> dict[str, int]:
    return {
        "ALLOCATOR ACTIONS": sum(1 for line in lines if "ALLOCATOR ACTIONS:" in line),
        f"ORDER_SKIP {_REASON}": sum(1 for line in lines if "ORDER_SKIP" in line and f"reason={_REASON}" in line),
        "DYNAMIC_ACCEPTED": sum(1 for line in lines if "DYNAMIC_ACCEPTED" in line),
        "DYNAMIC_SELECTED": sum(
            1
            for line in lines
            if "DYNAMIC_SELECTED" in line and "DYNAMIC_SELECTED_ENTRY" not in line
        ),
    }


def _has_required_log_evidence(lines: Sequence[str]) -> bool:
    counts = _grep_counts(lines)
    return counts["ALLOCATOR ACTIONS"] > 0 and counts[f"ORDER_SKIP {_REASON}"] > 0


def _load_log_lines(
    *,
    project_root: Path,
    data_dir: Path,
    day: str,
    log_text: str | None,
    log_files: Sequence[Path | str] | None,
) -> list[str]:
    if log_text is not None:
        lines = [line for line in log_text.splitlines() if _line_matches_day(line, day)]
        setattr(
            _load_log_lines,
            "_last_debug",
            {
                "LOG_SOURCE": "provided_log_text",
                "used_journalctl": False,
                "local_lines": len(lines),
                "journal_lines_total": 0,
                "grep_counts": _grep_counts(lines),
            },
        )
        return lines
    lines: list[str] = []
    discovered_paths = _discover_log_paths(project_root, data_dir=data_dir, day=day, extra_paths=log_files)
    for path in discovered_paths:
        try:
            lines.extend(_read_text(path).splitlines())
        except OSError:
            continue
    local_lines = [line for line in lines if _line_matches_day(line, day)]
    used_journalctl = False
    journal_lines_total = 0
    selected_lines = local_lines
    if not selected_lines or not _has_required_log_evidence(selected_lines):
        journal_lines = _journalctl_lines(day)
        journal_lines_total = len(journal_lines)
        journal_filtered = [line for line in journal_lines if _line_matches_day(line, day)]
        if journal_filtered:
            selected_lines = journal_filtered
            used_journalctl = True
    setattr(
        _load_log_lines,
        "_last_debug",
        {
            "LOG_SOURCE": "journalctl" if used_journalctl else "files",
            "used_journalctl": used_journalctl,
            "local_files": [str(path) for path in discovered_paths],
            "local_lines": len(local_lines),
            "journal_lines_total": journal_lines_total,
            "grep_counts": _grep_counts(selected_lines),
        },
    )
    return selected_lines


def _price_from_mapping(row: Mapping[str, Any]) -> float | None:
    for key in _PRICE_KEYS:
        value = _safe_float(row.get(key))
        if value is not None and value > 0:
            return value
    return None


def _symbol_from_mapping(row: Mapping[str, Any]) -> str:
    return str(row.get("symbol") or row.get("sym") or "").strip().upper()


def _symbol_from_line(line: str) -> str:
    kv = _parse_kv(line)
    symbol = str(kv.get("symbol") or kv.get("sym") or "").strip().upper()
    if symbol:
        return symbol
    match = re.search(r"\b(?:symbol|sym)=([A-Z][A-Z0-9.\-]{0,12})\b", line)
    return match.group(1).upper() if match else ""


def _field_first(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _normalize_timestamp(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_ET)
    return dt.astimezone(_ET)


def _allocator_payload_for_literal_eval(raw: str) -> str:
    # Production logs may contain numpy scalar reprs or JSON-ish non-finite values.
    text = re.sub(r"\b(?:np|numpy)\.float(?:16|32|64)?\(([^()]+)\)", r"\1", raw)
    text = re.sub(r"\b(?:np|numpy)\.int(?:16|32|64)?\(([^()]+)\)", r"\1", text)
    text = re.sub(r"\b(?:nan|NaN|inf|-inf|Infinity|-Infinity)\b", "None", text)
    return text


def _action_from_allocator_line(line: str) -> list[dict[str, Any]]:
    if "ALLOCATOR ACTIONS:" not in line:
        return []
    raw = line.split("ALLOCATOR ACTIONS:", 1)[1].strip()
    try:
        parsed = ast.literal_eval(_allocator_payload_for_literal_eval(raw))
    except (SyntaxError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(row) for row in parsed if isinstance(row, Mapping)]


def _scan_context_from_line(line: str, *, day: str) -> dict[str, Any] | None:
    if not any(marker in line for marker in ("DYNAMIC_ACCEPTED", "DYNAMIC_SELECTED", "DYNAMIC_SCAN accept")):
        return None
    if "DYNAMIC_SELECTED_ENTRY" in line:
        return None
    kv = _parse_kv(line)
    symbol = _symbol_from_line(line)
    if not symbol:
        match = re.search(r"\bDYNAMIC_SCAN accept\s+([A-Z][A-Z0-9.\-]{0,12})\b", line)
        symbol = match.group(1).upper() if match else ""
    if not symbol:
        return None
    ts = _parse_timestamp(line, day=day)
    return {
        "symbol": symbol,
        "_timestamp": ts,
        "entry_price": _price_from_mapping(kv),
        "relative_volume": _round(kv.get("relative_volume") or kv.get("rel_volume") or kv.get("rvol")),
        "gain_pct": _round(kv.get("gain_pct") or kv.get("day_gain_pct") or kv.get("gain")),
        "catalyst_age_minutes": _round(kv.get("catalyst_age_minutes") or kv.get("age_minutes")),
        "market_regime": kv.get("market_regime") or kv.get("regime"),
        "news_score": _round(kv.get("news_score")),
        "event_score": _round(kv.get("event_score")),
        "catalyst_score": _round(kv.get("catalyst_score")),
        "spread_pct": _round(kv.get("spread_pct") or kv.get("spread")),
        "atr_pct": _round(kv.get("atr_pct") or kv.get("atr_percent")),
        "entry_eval_pass": _field_first(kv.get("entry_eval_final"), kv.get("decision_allowed"), kv.get("final")),
        "scan_context_line": line,
    }


def _nearest_prior_context(
    contexts: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    symbol: str,
    timestamp: datetime | None,
    max_age_seconds: float = 300.0,
) -> Mapping[str, Any] | None:
    candidates = list(contexts.get(symbol, []))
    if not candidates:
        return None
    event_ts = _normalize_timestamp(timestamp)
    if event_ts is None:
        return candidates[-1]
    prior: list[tuple[float, Mapping[str, Any]]] = []
    no_timestamp: list[Mapping[str, Any]] = []
    for context in candidates:
        context_ts = _normalize_timestamp(context.get("_timestamp") if isinstance(context.get("_timestamp"), datetime) else None)
        if not isinstance(context_ts, datetime):
            no_timestamp.append(context)
            continue
        delta = (event_ts - context_ts).total_seconds()
        if 0 <= delta <= max_age_seconds:
            prior.append((delta, context))
    if prior:
        prior.sort(key=lambda row: row[0])
        return prior[0][1]
    return no_timestamp[-1] if event_ts is None and no_timestamp else None


def _debug_context_row(context: Mapping[str, Any], *, kind: str) -> dict[str, Any]:
    ts = _normalize_timestamp(context.get("_timestamp") if isinstance(context.get("_timestamp"), datetime) else None)
    return {
        "kind": kind,
        "timestamp": ts.isoformat() if ts is not None else None,
        "symbol": str(context.get("symbol") or ""),
        "entry_price": _round(_price_from_mapping(context)),
        "relative_volume": _round(
            _field_first(
                context.get("relative_volume"),
                context.get("rel_volume"),
                context.get("scanner_relative_volume"),
                context.get("entry_relative_volume"),
                context.get("allocator_relative_volume"),
            )
        ),
        "gain_pct": _round(context.get("gain_pct") or context.get("day_gain_pct")),
        "catalyst_age_minutes": _round(context.get("catalyst_age_minutes") or context.get("age_minutes")),
        "market_regime": context.get("market_regime") or context.get("regime"),
    }


def _event_from_order_skip_line(
    line: str,
    *,
    day: str,
    allocator_contexts: Mapping[str, Sequence[Mapping[str, Any]]],
    scan_contexts: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    if "ORDER_SKIP" not in line or f"reason={_REASON}" not in line:
        return None
    kv = _parse_kv(line)
    symbol = str(kv.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    ts = _parse_timestamp(line, day=day)
    action = _nearest_prior_context(allocator_contexts, symbol=symbol, timestamp=ts) or {}
    scan = _nearest_prior_context(scan_contexts, symbol=symbol, timestamp=ts) or {}
    entry_price = _price_from_mapping(kv) or _price_from_mapping(action) or _price_from_mapping(scan)
    return {
        "symbol": symbol,
        "entry_time": ts.astimezone(_ET).isoformat() if ts is not None else None,
        "entry_price": _round(entry_price),
        "relative_volume": _round(
            _field_first(
                kv.get("relative_volume"),
                kv.get("rel_volume"),
                action.get("relative_volume"),
                action.get("rel_volume"),
                action.get("scanner_relative_volume"),
                action.get("entry_relative_volume"),
                action.get("allocator_relative_volume"),
                scan.get("relative_volume"),
                scan.get("rel_volume"),
                scan.get("scanner_relative_volume"),
            )
        ),
        "gain_pct": _round(
            _field_first(
                kv.get("gain_pct"),
                kv.get("day_gain_pct"),
                action.get("gain_pct"),
                action.get("day_gain_pct"),
                scan.get("gain_pct"),
                scan.get("day_gain_pct"),
            )
        ),
        "catalyst_age_minutes": _round(
            _field_first(
                kv.get("catalyst_age_minutes"),
                kv.get("age_minutes"),
                action.get("catalyst_age_minutes"),
                action.get("age_minutes"),
                scan.get("catalyst_age_minutes"),
                scan.get("age_minutes"),
            )
        ),
        "market_regime": _field_first(
            kv.get("market_regime"),
            kv.get("regime"),
            action.get("market_regime"),
            action.get("regime"),
            scan.get("market_regime"),
            scan.get("regime"),
        ),
        "news_score": _round(_field_first(kv.get("news_score"), action.get("news_score"), scan.get("news_score"))),
        "event_score": _round(_field_first(kv.get("event_score"), action.get("event_score"), scan.get("event_score"))),
        "catalyst_score": _round(_field_first(kv.get("catalyst_score"), action.get("catalyst_score"), scan.get("catalyst_score"))),
        "spread_pct": _round(
            _field_first(
                kv.get("spread_pct"),
                kv.get("spread"),
                action.get("spread_pct"),
                action.get("spread"),
                scan.get("spread_pct"),
                scan.get("spread"),
            )
        ),
        "atr_pct": _round(
            _field_first(
                kv.get("atr_pct"),
                kv.get("atr_percent"),
                action.get("atr_pct"),
                action.get("atr_percent"),
                action.get("dynamic_atr_pct"),
                scan.get("atr_pct"),
                scan.get("atr_percent"),
            )
        ),
        "entry_eval_pass": _field_first(
            kv.get("entry_eval_final"),
            kv.get("decision_allowed"),
            action.get("entry_eval_final"),
            action.get("decision_allowed"),
            action.get("final"),
            scan.get("entry_eval_final"),
            scan.get("decision_allowed"),
        ),
        "matched_allocator_context": bool(action),
        "matched_scan_context": bool(scan),
        "source": "logs",
        "raw_line": line,
    }


def _events_from_logs(lines: Sequence[str], *, day: str) -> list[dict[str, Any]]:
    allocator_contexts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    scan_contexts: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    events: list[dict[str, Any]] = []
    parsed_allocator_actions: list[dict[str, Any]] = []
    parsed_order_skips: list[dict[str, Any]] = []
    for line in lines:
        ts = _parse_timestamp(line, day=day)
        scan_context = _scan_context_from_line(line, day=day)
        if scan_context is not None:
            scan_contexts[str(scan_context.get("symbol") or "")].append(scan_context)
        for action in _action_from_allocator_line(line):
            symbol = _symbol_from_mapping(action)
            if symbol:
                context = dict(action)
                context["_timestamp"] = ts
                allocator_contexts[symbol].append(context)
                parsed_allocator_actions.append(_debug_context_row(context, kind="allocator_action"))
        event = _event_from_order_skip_line(
            line,
            day=day,
            allocator_contexts=allocator_contexts,
            scan_contexts=scan_contexts,
        )
        if event is not None:
            events.append(event)
            parsed_order_skips.append(
                {
                    "timestamp": event.get("entry_time"),
                    "symbol": event.get("symbol"),
                    "matched_allocator_context": event.get("matched_allocator_context"),
                    "matched_scan_context": event.get("matched_scan_context"),
                    "entry_price": event.get("entry_price"),
                    "relative_volume": event.get("relative_volume"),
                    "gain_pct": event.get("gain_pct"),
                    "catalyst_age_minutes": event.get("catalyst_age_minutes"),
                }
            )
    setattr(
        _events_from_logs,
        "_last_debug",
        {
            "parsed_allocator_actions": parsed_allocator_actions,
            "parsed_order_skips": parsed_order_skips,
        },
    )
    return events


def _events_from_attribution(*, data_dir: Path, user_id: str, day: str) -> list[dict[str, Any]]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload.get("orders") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        return []
    events: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("reject_reason") != _REASON:
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        ts = _parse_timestamp(row.get("timestamp"), day=day)
        if not symbol:
            continue
        events.append(
            {
                "symbol": symbol,
                "entry_time": ts.astimezone(_ET).isoformat() if ts is not None else None,
                "entry_price": _round(_price_from_mapping(row)),
                "relative_volume": _round(row.get("relative_volume") or row.get("rel_volume")),
                "gain_pct": _round(row.get("gain_pct") or row.get("day_gain_pct")),
                "catalyst_age_minutes": _round(row.get("catalyst_age_minutes")),
                "market_regime": row.get("market_regime") or row.get("regime"),
                "news_score": _round(row.get("news_score")),
                "event_score": _round(row.get("event_score")),
                "catalyst_score": _round(row.get("catalyst_score")),
                "spread_pct": _round(row.get("spread_pct") or row.get("spread")),
                "atr_pct": _round(row.get("atr_pct") or row.get("atr_percent")),
                "entry_eval_pass": row.get("entry_eval_final") or row.get("decision_allowed") or row.get("final"),
                "matched_allocator_context": False,
                "matched_scan_context": False,
                "source": "trade_attribution",
                "raw_line": None,
            }
        )
    return events


def _dedupe_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in events:
        key = (str(row.get("symbol") or ""), str(row.get("entry_time") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def _candidate_bar_files(data_dir: Path, symbol: str, day: str, bars_dir: Path | None) -> list[Path]:
    if bars_dir is not None:
        roots = [bars_dir]
    else:
        roots = [
            *expected_bar_dirs(data_dir),
            data_dir / "research" / "dynamic_candidate_bars",
            data_dir / "research" / "allocator_candidate_bars",
        ]
    compact = day.replace("-", "")
    patterns: list[str] = []
    for suffix in ("csv", "json"):
        patterns.extend(
            [
                f"**/{symbol}*{day}*.{suffix}",
                f"**/{day}*{symbol}*.{suffix}",
                f"**/{symbol}*{compact}*.{suffix}",
                f"**/{compact}*{symbol}*.{suffix}",
                f"**/{day}/**/{symbol}.{suffix}",
                f"**/{compact}/**/{symbol}.{suffix}",
            ]
        )
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            paths.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(dict.fromkeys(paths))


def _load_bars(data_dir: Path, *, symbol: str, day: str, bars_dir: Path | None, cache: dict[str, pd.DataFrame | None]) -> pd.DataFrame | None:
    if symbol in cache:
        return cache[symbol]
    frames: list[pd.DataFrame] = []
    for path in _candidate_bar_files(data_dir, symbol, day, bars_dir):
        _, frame = _read_bar_file(path)
        if frame is None or frame.empty:
            continue
        ts = _bar_timestamps(frame)
        if ts is None:
            continue
        work = frame.copy()
        work["_timestamp"] = pd.to_datetime(ts, utc=True, errors="coerce")
        work = work[work["_timestamp"].notna()]
        if work.empty:
            continue
        if "close" not in work.columns:
            continue
        frames.append(work)
    if not frames:
        cache[symbol] = None
        return None
    bars = pd.concat(frames, ignore_index=True)
    bars = bars.sort_values("_timestamp").drop_duplicates(subset=["_timestamp"], keep="last")
    day_mask = bars["_timestamp"].dt.tz_convert(_ET).dt.date.astype(str) == day
    bars = bars.loc[day_mask].reset_index(drop=True)
    cache[symbol] = bars if not bars.empty else None
    return cache[symbol]


def _first_close_at_or_after(bars: pd.DataFrame, ts: datetime) -> float | None:
    matches = bars.loc[bars["_timestamp"] >= pd.Timestamp(ts.astimezone(_UTC))]
    if matches.empty:
        return None
    return _safe_float(matches.iloc[0].get("close"))


def _forward_metrics(
    row: Mapping[str, Any],
    *,
    data_dir: Path,
    day: str,
    bars_dir: Path | None,
    cache: dict[str, pd.DataFrame | None],
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").strip().upper()
    entry_dt = _parse_timestamp(row.get("entry_time"), day=day)
    bars = _load_bars(data_dir, symbol=symbol, day=day, bars_dir=bars_dir, cache=cache)
    if entry_dt is None or bars is None or bars.empty:
        return {
            "forward_returns_available": False,
            "entry_price_source": "missing",
            "missing_forward_bar_reason": "missing_entry_time" if entry_dt is None else "missing_forward_bars",
            **{f"return_{minutes}m_pct": None for minutes in _HORIZONS_MINUTES},
            "max_drawdown_pct": None,
            "max_excursion_pct": None,
        }
    entry_price = _safe_float(row.get("entry_price"))
    entry_price_source = "log"
    if entry_price is None or entry_price <= 0:
        entry_price = _first_close_at_or_after(bars, entry_dt)
        entry_price_source = "bars"
    if entry_price is None or entry_price <= 0:
        return {
            "forward_returns_available": False,
            "entry_price_source": "missing",
            "missing_forward_bar_reason": "missing_entry_price",
            **{f"return_{minutes}m_pct": None for minutes in _HORIZONS_MINUTES},
            "max_drawdown_pct": None,
            "max_excursion_pct": None,
        }
    out: dict[str, Any] = {
        "entry_price": _round(entry_price),
        "entry_price_source": entry_price_source,
        "missing_forward_bar_reason": None,
    }
    for minutes in _HORIZONS_MINUTES:
        close = _first_close_at_or_after(bars, entry_dt + timedelta(minutes=minutes))
        out[f"return_{minutes}m_pct"] = _round(((close - entry_price) / entry_price) * 100.0) if close is not None else None
    window = bars.loc[
        (bars["_timestamp"] >= pd.Timestamp(entry_dt.astimezone(_UTC)))
        & (bars["_timestamp"] <= pd.Timestamp((entry_dt + timedelta(minutes=60)).astimezone(_UTC)))
    ]
    low = _safe_float(window["low"].min()) if not window.empty and "low" in window.columns else None
    high = _safe_float(window["high"].max()) if not window.empty and "high" in window.columns else None
    out["max_drawdown_pct"] = _round(((low - entry_price) / entry_price) * 100.0) if low is not None else None
    out["max_excursion_pct"] = _round(((high - entry_price) / entry_price) * 100.0) if high is not None else None
    out["forward_returns_available"] = any(out.get(f"return_{minutes}m_pct") is not None for minutes in _HORIZONS_MINUTES)
    return out


def _bucket_rvol(value: Any) -> str:
    rvol = _safe_float(value)
    if rvol is None:
        return "missing"
    if 0.3 <= rvol < 0.5:
        return "0.3-0.5"
    if 0.5 <= rvol < 0.8:
        return "0.5-0.8"
    if 0.8 <= rvol < 1.2:
        return "0.8-1.2"
    if rvol >= 1.2:
        return ">1.2"
    return "<0.3"


def _bucket_gain(value: Any) -> str:
    gain = _safe_float(value)
    if gain is None:
        return "missing"
    if gain < 5:
        return "<5%"
    if gain < 10:
        return "5-10%"
    if gain < 20:
        return "10-20%"
    return ">20%"


def _bucket_catalyst_age(value: Any) -> str:
    age = _safe_float(value)
    if age is None:
        return "missing"
    if age < 30:
        return "<30m"
    if age < 120:
        return "30-120m"
    if age < 390:
        return "120-390m"
    return ">390m"


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 4)
    return round((ordered[mid - 1] + ordered[mid]) / 2.0, 4)


def _return_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for minutes in _HORIZONS_MINUTES:
        field = f"return_{minutes}m_pct"
        values = [float(value) for row in rows if (value := _safe_float(row.get(field))) is not None]
        summary[f"{minutes}m"] = {
            "available": len(values),
            "average_return_pct": _mean(values),
            "median_return_pct": _median(values),
            "win_rate": round(len([value for value in values if value > 0.0]) / len(values), 4) if values else None,
        }
    drawdowns = [float(value) for row in rows if (value := _safe_float(row.get("max_drawdown_pct"))) is not None]
    excursions = [float(value) for row in rows if (value := _safe_float(row.get("max_excursion_pct"))) is not None]
    summary["max_drawdown_pct"] = min(drawdowns) if drawdowns else None
    summary["max_excursion_pct"] = max(excursions) if excursions else None
    return summary


def _group_summary(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "missing")].append(row)
    return {
        name: {
            "count": len(items),
            "symbols": sorted({str(item.get("symbol") or "") for item in items if item.get("symbol")}),
            "returns": _return_summary(items),
        }
        for name, items in sorted(grouped.items())
    }


def _load_default_config(project_root: Path) -> dict[str, Any]:
    path = project_root / "config" / "default.yaml"
    if not path.exists():
        return {}
    try:
        return load_config(path)
    except Exception:
        return {}


def _exception_experiment_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    cfg = config if isinstance(config, Mapping) else {}
    du_cfg = cfg.get("dynamic_universe") if isinstance(cfg.get("dynamic_universe"), Mapping) else {}
    raw = du_cfg.get("live_weak_catalyst_exception_experiment") if isinstance(du_cfg, Mapping) else {}
    exp = raw if isinstance(raw, Mapping) else {}
    return {
        "enabled": bool(exp.get("enabled", False)),
        "min_price": _safe_float(exp.get("min_price")) or 8.0,
        "min_gain_pct": _safe_float(exp.get("min_gain_pct")) or 10.0,
        "min_relative_volume": _safe_float(exp.get("min_relative_volume")) or 0.5,
        "max_spread_pct": _safe_float(exp.get("max_spread_pct")) or 0.25,
        "require_entry_eval_pass": bool(exp.get("require_entry_eval_pass", True)),
        "max_atr_pct": _safe_float(exp.get("max_atr_pct")) or 15.0,
        "max_positions_per_day": int(_safe_float(exp.get("max_positions_per_day")) or 1),
        "notional_cap": _safe_float(exp.get("notional_cap")) or 300.0,
        "require_no_existing_position": bool(exp.get("require_no_existing_position", True)),
    }


def _entry_eval_passed(row: Mapping[str, Any]) -> bool:
    for key in ("entry_eval_final", "decision_allowed", "final", "entry_final", "entry_eval_pass"):
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    return False


def _exception_eligibility(row: Mapping[str, Any], config: Mapping[str, Any] | None, *, used_today: int) -> tuple[bool, str]:
    exp = _exception_experiment_config(config)
    if not exp["enabled"]:
        return False, "experiment_disabled"
    price = _safe_float(row.get("entry_price"))
    if price is None or price < float(exp["min_price"]):
        return False, "price_below_min"
    gain = _safe_float(row.get("gain_pct"))
    if gain is None or gain < float(exp["min_gain_pct"]):
        return False, "gain_below_min"
    rvol = _safe_float(row.get("relative_volume"))
    if rvol is None or rvol < float(exp["min_relative_volume"]):
        return False, "relative_volume_below_min"
    spread = _safe_float(row.get("spread_pct"))
    if spread is None or spread > float(exp["max_spread_pct"]):
        return False, "spread_above_max"
    if exp["require_entry_eval_pass"] and not _entry_eval_passed(row):
        return False, "entry_eval_not_passed"
    atr_pct = _safe_float(row.get("atr_pct"))
    if atr_pct is None:
        return False, "atr_unavailable"
    if atr_pct > float(exp["max_atr_pct"]):
        return False, "atr_above_max"
    if exp["require_no_existing_position"] and bool(row.get("existing_position")):
        return False, "existing_position"
    if used_today >= int(exp["max_positions_per_day"]):
        return False, "daily_max_positions_per_day"
    return True, "qualified"


def build_dynamic_weak_catalyst_outcomes(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    bars_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build weak-catalyst what-if outcomes from logs, attribution, and local minute bars."""
    day_s = _day_text(day)
    root = Path(project_root)
    data = Path(data_dir)
    cfg = config if isinstance(config, Mapping) else _load_default_config(root)
    bars_path = Path(bars_dir) if bars_dir is not None else None
    lines = _load_log_lines(project_root=root, data_dir=data, day=day_s, log_text=log_text, log_files=log_files)
    source_debug = getattr(_load_log_lines, "_last_debug", {})
    log_events = _events_from_logs(lines, day=day_s)
    log_debug = getattr(_events_from_logs, "_last_debug", {})
    events = _dedupe_events([
        *log_events,
        *_events_from_attribution(data_dir=data, user_id=user_id, day=day_s),
    ])
    cache: dict[str, pd.DataFrame | None] = {}
    rows: list[dict[str, Any]] = []
    exception_used_today = 0
    for event in events:
        row = dict(event)
        row["missing_entry_before_bar_fallback"] = _safe_float(row.get("entry_price")) is None
        row.update(_forward_metrics(row, data_dir=data, day=day_s, bars_dir=bars_path, cache=cache))
        row["rvol_bucket"] = _bucket_rvol(row.get("relative_volume"))
        row["gain_bucket"] = _bucket_gain(row.get("gain_pct"))
        row["catalyst_age_bucket"] = _bucket_catalyst_age(row.get("catalyst_age_minutes"))
        row["market_regime"] = str(row.get("market_regime") or "missing")
        qualifies, reason = _exception_eligibility(row, cfg, used_today=exception_used_today)
        row["live_weak_catalyst_exception_qualifies"] = bool(qualifies)
        row["live_weak_catalyst_exception_reason"] = reason
        row["live_weak_catalyst_exception_notional_cap"] = _exception_experiment_config(cfg)["notional_cap"]
        if qualifies:
            exception_used_today += 1
        rows.append(row)
    available_rows = [row for row in rows if row.get("forward_returns_available")]
    return {
        "report": "dynamic_weak_catalyst_outcomes",
        "research_only": True,
        "date": day_s,
        "user_id": str(user_id or "default"),
        "reason": _REASON,
        "summary": {
            "skips": len(rows),
            "symbols": sorted({str(row.get("symbol") or "") for row in rows if row.get("symbol")}),
            "symbols_count": len({str(row.get("symbol") or "") for row in rows if row.get("symbol")}),
            "forward_returns_available": len(available_rows),
            "missing_forward_returns": len(rows) - len(available_rows),
            "debug_counts": {
                "matched_allocator_context": len([row for row in rows if row.get("matched_allocator_context")]),
                "matched_scan_context": len([row for row in rows if row.get("matched_scan_context")]),
                "missing_entry": len([row for row in rows if row.get("missing_entry_before_bar_fallback")]),
                "missing_forward_bars": len(
                    [
                        row
                        for row in rows
                        if row.get("missing_forward_bar_reason") == "missing_forward_bars"
                    ]
                ),
            },
            "returns": _return_summary(available_rows),
        },
        "debug": {
            "LOG_SOURCE": source_debug.get("LOG_SOURCE", "unknown"),
            "used_journalctl": bool(source_debug.get("used_journalctl", False)),
            "local_files": list(source_debug.get("local_files") or []),
            "local_lines": int(source_debug.get("local_lines") or 0),
            "journal_lines_total": int(source_debug.get("journal_lines_total") or 0),
            "grep_counts": dict(source_debug.get("grep_counts") or {}),
            "log_lines_read": len(lines),
            "parsed_allocator_actions": list(log_debug.get("parsed_allocator_actions") or []),
            "parsed_order_skips": list(log_debug.get("parsed_order_skips") or []),
        },
        "splits": {
            "rvol_bucket": _group_summary(rows, "rvol_bucket"),
            "gain_bucket": _group_summary(rows, "gain_bucket"),
            "catalyst_age": _group_summary(rows, "catalyst_age_bucket"),
            "market_regime": _group_summary(rows, "market_regime"),
        },
        "events": sorted(rows, key=lambda row: (str(row.get("entry_time") or ""), str(row.get("symbol") or ""))),
    }


def render_dynamic_weak_catalyst_outcomes(report: Mapping[str, Any]) -> str:
    """Render weak-catalyst outcome research as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    returns = summary.get("returns") if isinstance(summary.get("returns"), Mapping) else {}
    lines = [
        f"# Dynamic Weak Catalyst Outcomes {report.get('date')} user={report.get('user_id')}",
        "",
        "Read-only research: no trading behavior, risk controls, thresholds, sizing, orders, or exits changed.",
        "",
        "## Summary",
        f"- reason: {report.get('reason')}",
        f"- skips: {summary.get('skips', 0)}",
        f"- symbols: {', '.join(summary.get('symbols') or []) if summary.get('symbols') else 'none'}",
        f"- forward returns available: {summary.get('forward_returns_available', 0)}",
        f"- missing forward returns: {summary.get('missing_forward_returns', 0)}",
        "",
        "## Debug Counts",
    ]
    debug_counts = summary.get("debug_counts") if isinstance(summary.get("debug_counts"), Mapping) else {}
    for key in ("matched_allocator_context", "matched_scan_context", "missing_entry", "missing_forward_bars"):
        lines.append(f"- {key}: {debug_counts.get(key, 0)}")
    lines.extend(
        [
            "",
            "## Forward Returns",
            "| Horizon | Available | Average % | Median % | Win Rate |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for label in ("1m", "5m", "10m", "15m", "30m", "60m"):
        block = returns.get(label) if isinstance(returns.get(label), Mapping) else {}
        lines.append(
            f"| {label} | {block.get('available', 0)} | {block.get('average_return_pct')} | "
            f"{block.get('median_return_pct')} | {block.get('win_rate')} |"
        )
    lines.append(f"- max_drawdown_pct: {returns.get('max_drawdown_pct')}")
    lines.append(f"- max_excursion_pct: {returns.get('max_excursion_pct')}")
    lines.extend(["", "## Splits"])
    splits = report.get("splits") if isinstance(report.get("splits"), Mapping) else {}
    for split_name in ("rvol_bucket", "gain_bucket", "catalyst_age", "market_regime"):
        lines.append(f"### {split_name}")
        split = splits.get(split_name) if isinstance(splits.get(split_name), Mapping) else {}
        if not split:
            lines.append("- none")
            continue
        for bucket, block in split.items():
            ret = (block.get("returns") or {}).get("15m") if isinstance(block.get("returns"), Mapping) else {}
            symbols = ", ".join(block.get("symbols") or [])
            lines.append(
                f"- {bucket}: count={block.get('count')} avg_15m={ret.get('average_return_pct')} "
                f"win_15m={ret.get('win_rate')} symbols={symbols or 'none'}"
            )
    lines.extend(["", "## Events"])
    if not report.get("events"):
        lines.append("- none")
    for row in report.get("events") or []:
        lines.append(
            f"- {row.get('entry_time')} {row.get('symbol')} entry={row.get('entry_price')} "
            f"rvol={row.get('relative_volume')} gain={row.get('gain_pct')} "
            f"spread={row.get('spread_pct')} atr={row.get('atr_pct')} "
            f"exception_qualifies={row.get('live_weak_catalyst_exception_qualifies')} "
            f"exception_reason={row.get('live_weak_catalyst_exception_reason')} "
            f"1m={row.get('return_1m_pct')} 5m={row.get('return_5m_pct')} "
            f"15m={row.get('return_15m_pct')} 60m={row.get('return_60m_pct')} "
            f"dd={row.get('max_drawdown_pct')} mfe={row.get('max_excursion_pct')}"
        )
    return "\n".join(lines).rstrip() + "\n"


def write_dynamic_weak_catalyst_outcomes(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: date | str,
    user_id: str,
    bars_dir: Path | str | None = None,
    log_text: str | None = None,
    log_files: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write weak-catalyst outcome research artifacts."""
    data = Path(data_dir)
    day_s = _day_text(day)
    report = build_dynamic_weak_catalyst_outcomes(
        project_root=project_root,
        data_dir=data,
        day=day_s,
        user_id=user_id,
        bars_dir=bars_dir,
        log_text=log_text,
        log_files=log_files,
    )
    out_dir = data / "research_metrics" / day_s
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "dynamic_weak_catalyst_outcomes.json"
    text_path = out_dir / "dynamic_weak_catalyst_outcomes.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text_path.write_text(render_dynamic_weak_catalyst_outcomes(report), encoding="utf-8")
    return json_path, text_path, report


__all__ = [
    "WeakCatalystOutcomePaths",
    "build_dynamic_weak_catalyst_outcomes",
    "render_dynamic_weak_catalyst_outcomes",
    "write_dynamic_weak_catalyst_outcomes",
]
