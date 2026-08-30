"""Research-only allocator suppression outcome report."""

from __future__ import annotations

import ast
import gzip
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.dynamic_candidate_blockers import _load_local_bars_with_diagnostics, _outcome_from_bars

_ET = ZoneInfo("America/New_York")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_ENTRY_EVAL_RE = re.compile(
    r"\b(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\s+ENTRY_EVAL\s+"
    r"route=(?P<route>[^ ]+).*?\bfinal=(?P<final>[TF]|true|false|True|False)\b.*?"
    r"\breason=(?P<reason>.+?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|$)"
)
_SYMBOL_RE = re.compile(r"\bsymbol=(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b")
_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_COMPACT_DATE_RE = re.compile(r"(\d{8})")
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
class AllocatorSuppressionPaths:
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


def _line_timestamp(line: str, *, day: str) -> str | None:
    iso = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)", line)
    if iso:
        ts = _parse_timestamp(iso.group(1))
        return ts.isoformat() if ts else None
    match = re.match(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\b", line)
    if not match:
        return None
    expected = datetime.strptime(day, "%Y-%m-%d").date()
    month = _MONTHS.get(match.group("mon"))
    if month != expected.month or int(match.group("day")) != expected.day:
        return None
    hh, mm, ss = (int(part) for part in match.group("time").split(":"))
    return datetime(expected.year, expected.month, expected.day, hh, mm, ss, tzinfo=_ET).isoformat()


def _read_text(path: Path) -> str:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    return path.read_text(encoding="utf-8", errors="replace")


def _parse_kv(line: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",;") for match in _KV_RE.finditer(line)}


def _classify_reason(text: Any) -> str:
    raw = str(text or "").strip().lower()
    if "post_planner" in raw or "post-planner" in raw or "after_actions=none" in raw or "clipped_notional" in raw:
        return "post_planner_filter"
    if "cooldown" in raw or "re-entry" in raw or "reentry" in raw or "lockout" in raw:
        return "cooldown"
    if "add-on" in raw or "addon" in raw or "add-ons today" in raw or "max_adds" in raw:
        return "add_on_once_per_day"
    if "correlation" in raw or "correlated" in raw:
        return "correlation"
    if (
        "minimum_cash_to_deploy" in raw
        or "gross_headroom" in raw
        or "capital" in raw
        or "cash" in raw
        or "min_realloc" in raw
        or "sleeve" in raw
        or "allocator_returned_no_actions" in raw
    ):
        return "capital_limit"
    if (
        "risk" in raw
        or "daily_loss" in raw
        or "drawdown" in raw
        or "position cap" in raw
        or "symbol cap" in raw
        or "sector cap" in raw
        or "bucket" in raw
        or "exposure" in raw
    ):
        return "risk_limit"
    return "other"


def _action_symbols_from_text(text: str) -> set[str]:
    symbols: set[str] = set()
    for match in re.finditer(r"\b(?:buy|sell):([A-Z][A-Z0-9.\-]{0,9}):", text):
        symbols.add(match.group(1).upper())
    payload_match = re.search(r"(\[\{.+\}\])", text)
    if payload_match:
        try:
            payload = ast.literal_eval(payload_match.group(1))
        except Exception:
            payload = None
        if isinstance(payload, list):
            for row in payload:
                if isinstance(row, Mapping):
                    symbol = str(row.get("symbol") or "").strip().upper()
                    if symbol:
                        symbols.add(symbol)
    selected_match = re.search(r"selected=\[(?P<body>[^\]]*)\]", text)
    if selected_match:
        for symbol in re.findall(r"[A-Z][A-Z0-9.\-]{0,9}", selected_match.group("body")):
            symbols.add(symbol)
    symbols_match = re.search(r"\bsymbols=(?P<body>[^ ]+)", text)
    if symbols_match:
        for symbol in re.findall(r"[A-Z][A-Z0-9.\-]{0,9}", symbols_match.group("body")):
            symbols.add(symbol)
    return symbols


def _symbol_from_line(line: str) -> str | None:
    match = _SYMBOL_RE.search(line)
    if match:
        return match.group("symbol").upper()
    skip = re.search(r"\b(?:SKIP|ORDER_SKIP)\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9})\b", line)
    if skip:
        return skip.group("symbol").upper()
    return None


def _candidate_price(entry: Mapping[str, Any]) -> float | None:
    for key in ("price", "close", "last", "last_price"):
        value = _safe_float(entry.get(key))
        if value is not None and value > 0:
            return value
    return None


def _discover_log_paths(project_root: Path, *, day: str, extra_paths: Sequence[Path | str] | None) -> list[Path]:
    paths: list[Path] = []
    for base in (project_root / "data" / "logs", project_root / "data" / "debug_logs", project_root / "reports" / "debug"):
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


def _parse_logs(paths: Sequence[Path], *, day: str) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], set[str], list[str]]:
    entry_passed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    suppressions: list[dict[str, Any]] = []
    allocator_selected: set[str] = set()
    used: list[str] = []
    for path in paths:
        try:
            lines = _read_text(path).splitlines()
        except Exception:
            continue
        used.append(str(path))
        for line_no, line in enumerate(lines, start=1):
            ts = _line_timestamp(line, day=day)
            entry = _ENTRY_EVAL_RE.search(line)
            if entry and entry.group("final").lower() in {"t", "true"}:
                symbol = entry.group("symbol").upper()
                kv = _parse_kv(line)
                entry_passed[symbol].append(
                    {
                        "symbol": symbol,
                        "timestamp": ts,
                        "route": entry.group("route"),
                        "reason": entry.group("reason").strip(),
                        "price": _round(kv.get("price") or kv.get("close") or kv.get("last")),
                        "source_file": str(path),
                        "line_number": line_no,
                    }
                )
            if any(token in line for token in ("ALLOCATOR_CANDIDATE", "ALLOCATOR_INPUT", "ALLOCATOR ACTIONS", "selected=[")):
                allocator_selected.update(_action_symbols_from_text(line))
                sym = _symbol_from_line(line)
                if sym:
                    allocator_selected.add(sym)
            if "POST_PLANNER_ACTION_TRACE" in line:
                before = set()
                after = set()
                before_match = re.search(r"before_actions=(?P<body>[^ ]+)", line)
                after_match = re.search(r"after_actions=(?P<body>[^ ]+)", line)
                if before_match:
                    before = _action_symbols_from_text(before_match.group("body"))
                if after_match and after_match.group("body") != "none":
                    after = _action_symbols_from_text(after_match.group("body"))
                for symbol in sorted(before - after):
                    suppressions.append(
                        {
                            "symbol": symbol,
                            "timestamp": ts,
                            "suppression_type": "post_planner_filter",
                            "block_reason": "post_planner_removed_action",
                            "source_file": str(path),
                            "line_number": line_no,
                            "line": line.strip(),
                        }
                    )
                continue
            if any(
                token in line
                for token in (
                    "ALLOCATOR_SKIP",
                    "ALLOCATOR_REJECT",
                    "ALLOCATOR_NO_ACTION",
                    "ALLOCATOR_ACTION_BLOCKED",
                    "ORDER_SKIP",
                    "DYNAMIC_REENTRY_BLOCK",
                    "cooldown",
                    "minimum_cash_to_deploy",
                    "correlation",
                    "add-ons today",
                )
            ):
                symbol = _symbol_from_line(line)
                if not symbol:
                    continue
                kv = _parse_kv(line)
                reason = kv.get("reason") or kv.get("block_reason") or line.strip()
                suppressions.append(
                    {
                        "symbol": symbol,
                        "timestamp": ts,
                        "suppression_type": _classify_reason(f"{reason} {line}"),
                        "block_reason": str(reason),
                        "source_file": str(path),
                        "line_number": line_no,
                        "line": line.strip(),
                    }
                )
    return entry_passed, suppressions, allocator_selected, used


def _with_outcome(
    row: Mapping[str, Any],
    *,
    entry: Mapping[str, Any] | None,
    data_dir: Path,
    bars_dir: Path | str | None,
    day: str,
) -> dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    ts = _parse_timestamp(row.get("timestamp")) or _parse_timestamp((entry or {}).get("timestamp"))
    price = _candidate_price(entry or {})
    bars, diag = _load_local_bars_with_diagnostics(data_dir=data_dir, bars_dir=bars_dir, symbol=symbol, day=day)
    outcome = _outcome_from_bars(bars, rejected_at=ts, rejected_price=price, day=day)
    max_gain = (
        outcome.get("max_gain_after_block_pct")
        if outcome.get("max_gain_after_block_pct") is not None
        else outcome.get("max_gain_after_rejection_pct")
    )
    drawdown = _safe_float(outcome.get("max_drawdown_after_block_pct"))
    if drawdown is not None:
        drawdown = min(drawdown, 0.0)
    max_gain_number = _safe_float(max_gain)
    return {
        **row,
        "entry_eval": entry,
        "return_after_15m_pct": outcome.get("return_after_15m_pct"),
        "return_after_30m_pct": outcome.get("return_after_30m_pct"),
        "return_after_60m_pct": outcome.get("return_after_60m_pct"),
        "subsequent_15m_return_pct": outcome.get("return_after_15m_pct"),
        "subsequent_30m_return_pct": outcome.get("return_after_30m_pct"),
        "subsequent_60m_return_pct": outcome.get("return_after_60m_pct"),
        "max_gain_after_suppression_pct": max_gain,
        "max_drawdown_after_suppression_pct": drawdown,
        "time_to_max_gain_minutes": outcome.get("time_to_max_gain_minutes"),
        "reached_plus_1pct": None if max_gain_number is None else max_gain_number >= 1.0,
        "reached_plus_2pct": None if max_gain_number is None else max_gain_number >= 2.0,
        "reached_plus_3pct": None if max_gain_number is None else max_gain_number >= 3.0,
        "outcome_available": outcome.get("outcome_available"),
        "missing_reason": outcome.get("missing_reason"),
        "bar_diagnostics": diag,
    }


def _numbers(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = _safe_float(row.get(key))
        if value is not None:
            out.append(value)
    return out


def _mean(values: Sequence[float]) -> float | None:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return None
    return round(sum(finite) / len(finite), 4)


def _median(values: Sequence[float]) -> float | None:
    finite = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not finite:
        return None
    midpoint = len(finite) // 2
    if len(finite) % 2:
        return round(finite[midpoint], 4)
    return round((finite[midpoint - 1] + finite[midpoint]) / 2.0, 4)


def _percent(values: Sequence[bool]) -> float | None:
    if not values:
        return None
    return round((sum(1 for value in values if value) / len(values)) * 100.0, 2)


def _positive_percent(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    values = _numbers(rows, key)
    return _percent([value > 0.0 for value in values])


def _true_percent(rows: Sequence[Mapping[str, Any]], key: str) -> float | None:
    values = [bool(row.get(key)) for row in rows if row.get(key) is not None]
    return _percent(values)


def _effectiveness(summary: Mapping[str, Any]) -> dict[str, Any]:
    outcomes = int(summary.get("outcomes_available") or 0)
    if outcomes <= 0:
        return {
            "effectiveness_score": "unknown",
            "recommendation": "investigate further",
            "basis": "no local bars available for suppressed candidates",
        }
    avg_15m = _safe_float(summary.get("avg_15m_return"))
    avg_30m = _safe_float(summary.get("avg_30m_return"))
    avg_drawdown = _safe_float(summary.get("avg_drawdown"))
    avg_max_gain = _safe_float(summary.get("avg_max_gain"))
    hit_15m = _safe_float(summary.get("percent_positive_15m"))
    reached_2pct = _safe_float(summary.get("percent_reached_2pct"))
    drawdown_adjusted = None
    if avg_15m is not None and avg_drawdown is not None:
        drawdown_adjusted = round(avg_15m + avg_drawdown, 4)

    if (
        (avg_15m is not None and avg_15m >= 0.75)
        or (avg_30m is not None and avg_30m >= 1.0)
        or (hit_15m is not None and hit_15m >= 60.0)
        or (reached_2pct is not None and reached_2pct >= 40.0)
    ):
        score = "harmful"
        recommendation = "likely too restrictive"
    elif (
        (avg_15m is not None and avg_15m <= -0.25)
        and (hit_15m is not None and hit_15m <= 40.0)
        and (avg_max_gain is None or avg_max_gain < 1.0)
    ):
        score = "beneficial"
        recommendation = "keep as-is"
    else:
        score = "neutral"
        recommendation = "investigate further"
    return {
        "effectiveness_score": score,
        "recommendation": recommendation,
        "drawdown_adjusted_15m_return": drawdown_adjusted,
        "basis": (
            f"avg_15m={avg_15m} avg_30m={avg_30m} avg_drawdown={avg_drawdown} "
            f"avg_max_gain={avg_max_gain} hit_15m={hit_15m} reached_2pct={reached_2pct}"
        ),
    }


def _summary_by_type(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("suppression_type") or "unknown")].append(row)
    summaries: dict[str, dict[str, Any]] = {}
    for key, items in sorted(grouped.items()):
        max_gains = _numbers(items, "max_gain_after_suppression_pct")
        summary = {
            "count": len(items),
            "outcomes_available": sum(1 for row in items if row.get("outcome_available")),
            "missed_profitable_opportunities": sum(
                1 for row in items if (_safe_float(row.get("max_gain_after_suppression_pct")) or 0.0) > 0.0
            ),
            "average_15m_return_pct": _mean(_numbers(items, "subsequent_15m_return_pct")),
            "average_30m_return_pct": _mean(_numbers(items, "subsequent_30m_return_pct")),
            "average_60m_return_pct": _mean(_numbers(items, "subsequent_60m_return_pct")),
            "average_max_gain_after_suppression_pct": _mean(_numbers(items, "max_gain_after_suppression_pct")),
            "avg_max_gain": _mean(max_gains),
            "median_max_gain": _median(max_gains),
            "avg_drawdown": _mean(_numbers(items, "max_drawdown_after_suppression_pct")),
            "avg_15m_return": _mean(_numbers(items, "return_after_15m_pct")),
            "avg_30m_return": _mean(_numbers(items, "return_after_30m_pct")),
            "avg_60m_return": _mean(_numbers(items, "return_after_60m_pct")),
            "percent_positive_15m": _positive_percent(items, "return_after_15m_pct"),
            "percent_positive_30m": _positive_percent(items, "return_after_30m_pct"),
            "percent_positive_60m": _positive_percent(items, "return_after_60m_pct"),
            "percent_reached_1pct": _true_percent(items, "reached_plus_1pct"),
            "percent_reached_2pct": _true_percent(items, "reached_plus_2pct"),
            "percent_reached_3pct": _true_percent(items, "reached_plus_3pct"),
        }
        summary.update(_effectiveness(summary))
        summaries[key] = summary
    return summaries


def _outcome_compact(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "timestamp": row.get("timestamp"),
        "suppression_type": row.get("suppression_type"),
        "block_reason": row.get("block_reason"),
        "max_gain_after_suppression_pct": row.get("max_gain_after_suppression_pct"),
        "max_drawdown_after_suppression_pct": row.get("max_drawdown_after_suppression_pct"),
        "return_after_15m_pct": row.get("return_after_15m_pct"),
        "return_after_30m_pct": row.get("return_after_30m_pct"),
        "return_after_60m_pct": row.get("return_after_60m_pct"),
        "time_to_max_gain_minutes": row.get("time_to_max_gain_minutes"),
    }


def _symbol_analysis(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol:
            grouped[symbol].append(row)
    analysis: dict[str, dict[str, Any]] = {}
    for symbol, items in sorted(grouped.items()):
        available = [row for row in items if row.get("outcome_available")]
        best = max(available, key=lambda row: _safe_float(row.get("max_gain_after_suppression_pct")) or -float("inf"), default=None)
        worst = min(available, key=lambda row: _safe_float(row.get("return_after_15m_pct")) or 0.0, default=None)
        analysis[symbol] = {
            "suppressions": len(items),
            "outcomes_available": len(available),
            "average_outcome": {
                "avg_max_gain": _mean(_numbers(items, "max_gain_after_suppression_pct")),
                "avg_drawdown": _mean(_numbers(items, "max_drawdown_after_suppression_pct")),
                "avg_15m_return": _mean(_numbers(items, "return_after_15m_pct")),
                "avg_30m_return": _mean(_numbers(items, "return_after_30m_pct")),
                "avg_60m_return": _mean(_numbers(items, "return_after_60m_pct")),
            },
            "best_outcome": _outcome_compact(best) if best else None,
            "worst_outcome": _outcome_compact(worst) if worst else None,
        }
    for symbol in ("QQQ", "XLF", "XLE", "IWM", "JPM", "XLY"):
        analysis.setdefault(
            symbol,
            {
                "suppressions": 0,
                "outcomes_available": 0,
                "average_outcome": {
                    "avg_max_gain": None,
                    "avg_drawdown": None,
                    "avg_15m_return": None,
                    "avg_30m_return": None,
                    "avg_60m_return": None,
                },
                "best_outcome": None,
                "worst_outcome": None,
            },
        )
    return dict(sorted(analysis.items()))


def _recommendations_by_type(type_summary: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "effectiveness_score": value.get("effectiveness_score"),
            "recommendation": value.get("recommendation"),
            "basis": value.get("basis"),
        }
        for key, value in sorted(type_summary.items())
    }


def _compact(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "timestamp",
        "suppression_type",
        "block_reason",
        "subsequent_15m_return_pct",
        "subsequent_30m_return_pct",
        "subsequent_60m_return_pct",
        "max_gain_after_suppression_pct",
        "max_drawdown_after_suppression_pct",
        "time_to_max_gain_minutes",
        "reached_plus_1pct",
        "reached_plus_2pct",
        "reached_plus_3pct",
        "outcome_available",
        "missing_reason",
    )
    return {key: row.get(key) for key in keys}


def build_allocator_suppression_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    root = Path(project_root)
    data = Path(data_dir)
    discovered = _discover_log_paths(root, day=day, extra_paths=log_paths)
    entry_passed, suppressions, allocator_selected, used = _parse_logs(discovered, day=day)
    blocked: list[dict[str, Any]] = []
    for row in suppressions:
        symbol = str(row.get("symbol") or "").upper()
        entry = (entry_passed.get(symbol) or [None])[-1]
        blocked.append(_with_outcome(row, entry=entry, data_dir=data, bars_dir=bars_dir, day=day))
    blocked.sort(key=lambda row: (row.get("timestamp") or "", row.get("symbol") or "", row.get("suppression_type") or ""))
    winners = sorted(
        [row for row in blocked if (_safe_float(row.get("max_gain_after_suppression_pct")) or 0.0) > 0],
        key=lambda row: float(row.get("max_gain_after_suppression_pct") or 0.0),
        reverse=True,
    )
    symbols_with_bars = sorted(
        {
            str(row.get("symbol") or "").upper()
            for row in blocked
            if isinstance(row.get("bar_diagnostics"), Mapping) and row["bar_diagnostics"].get("found_file")
        }
    )
    symbols_missing_bars = sorted(
        {
            str(row.get("symbol") or "").upper()
            for row in blocked
            if not (isinstance(row.get("bar_diagnostics"), Mapping) and row["bar_diagnostics"].get("found_file"))
        }
    )
    bar_diag_by_symbol: dict[str, dict[str, Any]] = {}
    for row in blocked:
        symbol = str(row.get("symbol") or "").upper()
        diag = row.get("bar_diagnostics")
        if symbol and isinstance(diag, Mapping):
            bar_diag_by_symbol[symbol] = dict(diag)
    suppression_type_analysis = _summary_by_type(blocked)
    return {
        "report": "allocator_suppression",
        "research_only": True,
        "date": day,
        "user": user_id,
        "source_files": used,
        "entry_eval_passed": {
            "count": sum(len(rows) for rows in entry_passed.values()),
            "symbols": sorted(entry_passed),
            "events": [event for rows in entry_passed.values() for event in rows],
        },
        "allocator_selected": {
            "count": len(allocator_selected),
            "symbols": sorted(allocator_selected),
        },
        "blocked_candidates": blocked,
        "summary": {
            "blocked_candidates": len(blocked),
            "outcomes_available": sum(1 for row in blocked if row.get("outcome_available")),
            "suppression_type_counts": dict(Counter(str(row.get("suppression_type")) for row in blocked)),
            "missed_profitable_opportunities_by_suppression_type": {
                key: value["missed_profitable_opportunities"] for key, value in suppression_type_analysis.items()
            },
            "symbols_with_bars": symbols_with_bars,
            "symbols_missing_bars": symbols_missing_bars,
            "missing_bar_reason": {
                symbol: diag.get("missing_bar_reason") for symbol, diag in sorted(bar_diag_by_symbol.items())
            },
            "first_available_bar_time": min(
                (str(diag.get("first_available_bar_time")) for diag in bar_diag_by_symbol.values() if diag.get("first_available_bar_time")),
                default=None,
            ),
            "last_available_bar_time": max(
                (str(diag.get("last_available_bar_time")) for diag in bar_diag_by_symbol.values() if diag.get("last_available_bar_time")),
                default=None,
            ),
        },
        "bar_diagnostics": bar_diag_by_symbol,
        "suppression_type_analysis": suppression_type_analysis,
        "average_return_by_suppression_type": suppression_type_analysis,
        "symbol_analysis": _symbol_analysis(blocked),
        "recommendations": _recommendations_by_type(suppression_type_analysis),
        "top_suppressed_winners": [_compact(row) for row in winners[:25]],
    }


def render_allocator_suppression_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    entry = report.get("entry_eval_passed") if isinstance(report.get("entry_eval_passed"), Mapping) else {}
    selected = report.get("allocator_selected") if isinstance(report.get("allocator_selected"), Mapping) else {}
    lines = [
        f"Allocator Suppression Report - {report.get('date')} user={report.get('user')}",
        "Research-only: no trading behavior, entries, exits, allocator, sizing, risk controls, broker execution, or options logic changed.",
        "",
        "Summary",
        f"- source_files: {len(report.get('source_files') or [])}",
        f"- ENTRY_EVAL passed: {entry.get('count', 0)} symbols={', '.join(entry.get('symbols') or []) or 'none'}",
        f"- allocator selected: {selected.get('count', 0)} symbols={', '.join(selected.get('symbols') or []) or 'none'}",
        f"- blocked candidates: {summary.get('blocked_candidates', 0)}",
        f"- outcomes available: {summary.get('outcomes_available', 0)}",
        f"- suppression type counts: {summary.get('suppression_type_counts')}",
        f"- symbols with bars: {', '.join(summary.get('symbols_with_bars') or []) or 'none'}",
        f"- symbols missing bars: {', '.join(summary.get('symbols_missing_bars') or []) or 'none'}",
        f"- first available bar time: {summary.get('first_available_bar_time')}",
        f"- last available bar time: {summary.get('last_available_bar_time')}",
        "",
        "Average Return by Suppression Type",
    ]
    for key, block in sorted((report.get("average_return_by_suppression_type") or {}).items()):
        lines.append(f"- {key}: {block}")
    lines.append("")
    lines.append("Suppression Effectiveness")
    recommendations = report.get("recommendations") if isinstance(report.get("recommendations"), Mapping) else {}
    if not recommendations:
        lines.append("- none")
    for key, block in sorted(recommendations.items()):
        if not isinstance(block, Mapping):
            continue
        lines.append(
            f"- {key}: {block.get('effectiveness_score')} recommendation={block.get('recommendation')} "
            f"basis={block.get('basis')}"
        )
    lines.append("")
    lines.append("Symbol-Level Analysis")
    symbol_analysis = report.get("symbol_analysis") if isinstance(report.get("symbol_analysis"), Mapping) else {}
    for symbol in ("QQQ", "XLF", "XLE", "IWM", "JPM", "XLY"):
        block = symbol_analysis.get(symbol) if isinstance(symbol_analysis.get(symbol), Mapping) else {}
        lines.append(f"- {symbol}: {block}")
    lines.append("")
    lines.append("Top Suppressed Winners")
    winners = report.get("top_suppressed_winners") if isinstance(report.get("top_suppressed_winners"), list) else []
    if not winners:
        lines.append("- none")
    for row in winners[:15]:
        lines.append(
            "- {symbol} type={suppression_type} reason={block_reason} "
            "max_gain={max_gain_after_suppression_pct} r15={subsequent_15m_return_pct} "
            "r30={subsequent_30m_return_pct} r60={subsequent_60m_return_pct}".format(**row)
        )
    lines.append("")
    lines.append("Blocked Candidates")
    blocked = report.get("blocked_candidates") if isinstance(report.get("blocked_candidates"), list) else []
    if not blocked:
        lines.append("- none")
    for row in blocked[:100]:
        lines.append(
            "- {symbol} time={timestamp} type={suppression_type} reason={block_reason} "
            "max_gain={max_gain_after_suppression_pct} r15={subsequent_15m_return_pct} "
            "r30={subsequent_30m_return_pct} r60={subsequent_60m_return_pct} missing={missing_reason}".format(**row)
        )
    return "\n".join(lines) + "\n"


def write_allocator_suppression_report(
    *,
    project_root: Path | str = ".",
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    data = Path(data_dir)
    report = build_allocator_suppression_report(
        project_root=project_root,
        data_dir=data,
        day=day,
        user_id=user_id,
        bars_dir=bars_dir,
        log_paths=log_paths,
    )
    out_dir = data / "research" / "allocator_suppression"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{day}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_allocator_suppression_report(report), encoding="utf-8")
    return json_path, text_path, report
