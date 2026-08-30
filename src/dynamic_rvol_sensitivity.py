"""Research-only dynamic scanner RVOL threshold sensitivity report."""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("current_100", 1.00),
    ("relaxed_075", 0.75),
    ("relaxed_050", 0.50),
)
_LOG_SCAN_RE = re.compile(r"\bDYNAMIC_SCAN\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<body>.+)$")
_LOG_REJECT_RE = re.compile(r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<body>.+)$")
_KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")
_JOURNAL_TS_RE = re.compile(r"^(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})\b")
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
class DynamicRvolSensitivityPaths:
    """Artifact paths written for a dynamic RVOL sensitivity report."""

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
    num = _safe_float(value)
    return round(num, ndigits) if num is not None else None


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
    return dt


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


def _date_from_generated_at(payload: Mapping[str, Any], fallback_path: Path) -> str | None:
    ts = _parse_timestamp(payload.get("generated_at"))
    if ts is not None:
        return ts.astimezone(_ET).date().isoformat()
    return _date_from_path(fallback_path)


def _history_paths_for_date(history_dir: Path, *, day: str, user_id: str) -> tuple[list[Path], str]:
    """Return exact user paths, falling back to default history when needed."""
    if not history_dir.exists():
        return [], "none"
    safe_user = _safe_user(user_id)
    exact: list[Path] = []
    default: list[Path] = []
    for path in sorted(history_dir.glob("*.json")):
        if _date_from_path(path) != day:
            continue
        if path.name.endswith(f"_{safe_user}.json"):
            exact.append(path)
        elif path.name.endswith("_default.json"):
            default.append(path)
    if exact:
        return exact, "exact_user"
    return default, "default_fallback" if default else "none"


def latest_dynamic_rvol_sensitivity_date(
    *,
    data_dir: Path | str = "data",
    user_id: str = "paper_bot",
    history_dir: Path | str | None = None,
) -> str | None:
    """Return newest available dynamic scan-history date for a user or default fallback."""
    root = Path(history_dir) if history_dir is not None else Path(data_dir) / "dynamic_scan_history"
    if not root.exists():
        return None
    safe_user = _safe_user(user_id)
    exact_dates: set[str] = set()
    fallback_dates: set[str] = set()
    for path in root.glob("*.json"):
        day = _date_from_path(path)
        if day is None:
            continue
        if path.name.endswith(f"_{safe_user}.json"):
            exact_dates.add(day)
        elif path.name.endswith("_default.json"):
            fallback_dates.add(day)
    dates = exact_dates or fallback_dates
    return sorted(dates)[-1] if dates else None


def _load_json(path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _normalize_reason(reason: Any) -> str:
    text = str(reason or "").strip().lower().replace(" ", "_").replace("-", "_")
    if "relative_volume" in text or "rel_volume" in text:
        return "below_min_relative_volume"
    if "atr" in text and "expansion" in text:
        return "atr_expansion"
    if "unstable" in text and "quote" in text:
        return "unstable_quote"
    if "bad" in text and "quote" in text:
        return "bad_quote"
    if "day_gain" in text or "min_gain" in text:
        return "below_min_day_gain"
    if "avg_volume" in text:
        return "below_min_avg_volume"
    if "min_price" in text or "below_min_price" in text:
        return "below_min_price"
    if "spread" in text:
        return "spread_too_wide"
    if "gain_filter" in text or "gain_filter" in text.replace(" ", "_"):
        return "gain_filter"
    if "entry_alignment" in text:
        return "entry_alignment"
    if "cooldown" in text:
        return "cooldown"
    if "intraday_range" in text or "range_filter" in text:
        return "intraday_range"
    return text or "unknown"


def _parse_kv(body: str) -> dict[str, str]:
    return {match.group(1): match.group(2).rstrip(",;%") for match in _KV_RE.finditer(body)}


def _timestamp_from_log_line(line: str, *, day: str) -> str | None:
    match = _JOURNAL_TS_RE.search(line)
    if match is None:
        return None
    expected = datetime.strptime(day, "%Y-%m-%d").date()
    month = _MONTHS.get(match.group("mon"))
    if month != expected.month or int(match.group("day")) != expected.day:
        return None
    hh, mm, ss = (int(part) for part in match.group("time").split(":"))
    return datetime(expected.year, expected.month, expected.day, hh, mm, ss, tzinfo=_ET).isoformat()


def _candidate_timestamp(raw: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    ts = _parse_timestamp(raw.get("timestamp")) or _parse_timestamp(payload.get("generated_at"))
    return ts.astimezone(_ET).isoformat() if ts is not None else None


def _candidate_snapshot(
    raw: Mapping[str, Any],
    *,
    payload: Mapping[str, Any],
    path: Path,
    sequence: int,
) -> dict[str, Any]:
    quality = raw.get("quality") if isinstance(raw.get("quality"), Mapping) else {}
    reason = raw.get("rejection_reason") or quality.get("rejection_reason")
    rel_volume = _safe_float(raw.get("rel_volume"))
    if rel_volume is None:
        rel_volume = _safe_float(raw.get("relative_volume"))
    return {
        "symbol": str(raw.get("symbol") or "").strip().upper(),
        "timestamp": _candidate_timestamp(raw, payload),
        "accepted": bool(raw.get("accepted")),
        "price": _round(raw.get("price")),
        "gain_pct": _round(raw.get("gain_pct", raw.get("day_gain_pct"))),
        "rel_volume": _round(rel_volume),
        "spread_pct": _round(raw.get("spread_pct")),
        "avg_volume": _round(raw.get("avg_volume"), 2),
        "reject_reason": _normalize_reason(reason),
        "raw_reject_reason": str(reason or "").strip() or None,
        "atr_expansion_ratio": _round(quality.get("atr_expansion_ratio")),
        "news_score": _round(raw.get("news_score"), 2),
        "event_score": _round(raw.get("event_score"), 2),
        "catalyst_score": _round(raw.get("catalyst_score"), 4),
        "source_file": str(path),
        "source_sequence": sequence,
        "source_date": _date_from_generated_at(payload, path),
    }


def _log_scan_snapshot(
    *,
    symbol: str,
    body: str,
    timestamp: str | None,
    path: Path,
    line_number: int,
) -> dict[str, Any]:
    kv = _parse_kv(body)
    spread = _safe_float(kv.get("spread"))
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "accepted": False,
        "price": _round(kv.get("price")),
        "gain_pct": _round(kv.get("gain")),
        "rel_volume": _round(kv.get("rel")),
        "spread_pct": _round(kv.get("spread_pct") or spread),
        "avg_volume": _round(kv.get("avg"), 2),
        "reject_reason": None,
        "raw_reject_reason": None,
        "atr_expansion_ratio": _round(kv.get("atr_exp")),
        "news_score": _round(kv.get("news_score"), 2),
        "event_score": None,
        "catalyst_score": None,
        "source_file": str(path),
        "source_sequence": line_number,
        "source_date": None,
    }


def _log_reject_snapshot(
    *,
    symbol: str,
    body: str,
    timestamp: str | None,
    previous_scan: Mapping[str, Any] | None,
    path: Path,
    line_number: int,
) -> dict[str, Any]:
    kv = _parse_kv(body)
    row = dict(previous_scan or {})
    row.update(
        {
            "symbol": symbol,
            "timestamp": row.get("timestamp") or timestamp,
            "accepted": False,
            "reject_reason": _normalize_reason(body),
            "raw_reject_reason": body.strip(),
            "source_file": str(path),
            "source_sequence": line_number,
        }
    )
    if row.get("price") is None:
        row["price"] = _round(kv.get("price"))
    if row.get("gain_pct") is None:
        row["gain_pct"] = _round(kv.get("gain"))
    if row.get("rel_volume") is None:
        row["rel_volume"] = _round(kv.get("rel") or kv.get("relative_volume"))
    if row.get("spread_pct") is None:
        row["spread_pct"] = _round(kv.get("spread") or kv.get("spread_pct"))
    if row.get("avg_volume") is None:
        row["avg_volume"] = _round(kv.get("avg"), 2)
    if row.get("catalyst_score") is None:
        row["catalyst_score"] = _round(kv.get("catalyst_score"), 4)
    row.setdefault("atr_expansion_ratio", None)
    row.setdefault("news_score", None)
    row.setdefault("event_score", None)
    row.setdefault("source_date", None)
    return row


def _load_scan_candidates(paths: Sequence[Path], *, day: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sequence = 0
    for path in paths:
        payload = _load_json(path)
        if payload is None:
            continue
        if _date_from_generated_at(payload, path) != day:
            continue
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            continue
        for raw in candidates:
            if not isinstance(raw, Mapping):
                continue
            sequence += 1
            snapshot = _candidate_snapshot(raw, payload=payload, path=path, sequence=sequence)
            if snapshot["symbol"]:
                rows.append(snapshot)
    return rows


def _load_log_candidates(log_paths: Sequence[Path], *, day: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in log_paths:
        if not path.exists() or not path.is_file():
            continue
        pending: dict[str, dict[str, Any]] = {}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines, start=1):
            timestamp = _timestamp_from_log_line(line, day=day)
            reject_match = _LOG_REJECT_RE.search(line)
            if reject_match is not None:
                symbol = reject_match.group("symbol").upper()
                row = _log_reject_snapshot(
                    symbol=symbol,
                    body=reject_match.group("body"),
                    timestamp=timestamp,
                    previous_scan=pending.get(symbol),
                    path=path,
                    line_number=line_number,
                )
                row["source_date"] = day
                rows.append(row)
                continue
            scan_match = _LOG_SCAN_RE.search(line)
            if scan_match is not None:
                symbol = scan_match.group("symbol").upper()
                row = _log_scan_snapshot(
                    symbol=symbol,
                    body=scan_match.group("body"),
                    timestamp=timestamp,
                    path=path,
                    line_number=line_number,
                )
                row["source_date"] = day
                pending[symbol] = row
    return rows


def _bar_roots(data_dir: Path, bars_dir: Path | str | None) -> list[Path]:
    if bars_dir is not None:
        return [Path(bars_dir)]
    return [
        data_dir / "historical_bars",
        data_dir / "bars",
        data_dir / "market_bars",
        data_dir / "intraday_bars",
    ]


def _read_bar_rows(path: Path) -> list[dict[str, Any]]:
    try:
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8", newline="") as fh:
                return [dict(row) for row in csv.DictReader(fh)]
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = loaded.get("bars") if isinstance(loaded, Mapping) else loaded
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _load_bar_rows(
    *,
    data_dir: Path,
    bars_dir: Path | str | None,
    symbol: str,
    day: str,
) -> list[dict[str, Any]]:
    compact = day.replace("-", "")
    candidates: list[Path] = []
    for root in _bar_roots(data_dir, bars_dir):
        if not root.exists():
            continue
        for suffix in ("csv", "json"):
            candidates.extend(root.glob(f"**/{symbol}*{day}*.{suffix}"))
            candidates.extend(root.glob(f"**/{day}*{symbol}*.{suffix}"))
            candidates.extend(root.glob(f"**/{symbol}*{compact}*.{suffix}"))
            candidates.extend(root.glob(f"**/{compact}*{symbol}*.{suffix}"))
            candidates.extend(root.glob(f"**/{symbol}.{suffix}"))
    parsed: list[dict[str, Any]] = []
    for path in sorted(dict.fromkeys(candidates)):
        for raw in _read_bar_rows(path):
            ts = _parse_timestamp(
                raw.get("timestamp") or raw.get("datetime") or raw.get("time") or raw.get("ts") or raw.get("t")
            )
            close = _safe_float(raw.get("close") or raw.get("Close") or raw.get("c"))
            if ts is None or close is None:
                continue
            if ts.astimezone(_ET).date().isoformat() != day:
                continue
            parsed.append({"timestamp": ts.astimezone(_UTC), "close": close})
        if parsed:
            break
    return sorted(parsed, key=lambda row: row["timestamp"])


def _first_close_at_or_after(rows: Sequence[dict[str, Any]], target: datetime) -> float | None:
    target_utc = target.astimezone(_UTC)
    for row in rows:
        ts = row.get("timestamp")
        if isinstance(ts, datetime) and ts >= target_utc:
            return _safe_float(row.get("close"))
    return None


def _forward_returns(
    row: Mapping[str, Any],
    *,
    data_dir: Path,
    bars_dir: Path | str | None,
    day: str,
    cache: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    observed_price = _safe_float(row.get("price"))
    observed_at = _parse_timestamp(row.get("timestamp"))
    symbol = str(row.get("symbol") or "").strip().upper()
    if observed_price is None or observed_price <= 0 or observed_at is None or not symbol:
        return {
            "forward_returns_available": False,
            "return_15m_pct": None,
            "return_30m_pct": None,
            "return_60m_pct": None,
            "return_eod_pct": None,
        }
    if symbol not in cache:
        cache[symbol] = _load_bar_rows(data_dir=data_dir, bars_dir=bars_dir, symbol=symbol, day=day)
    bars = cache[symbol]
    if not bars:
        return {
            "forward_returns_available": False,
            "return_15m_pct": None,
            "return_30m_pct": None,
            "return_60m_pct": None,
            "return_eod_pct": None,
        }

    def pct(close: float | None) -> float | None:
        return round(((close / observed_price) - 1.0) * 100.0, 4) if close is not None else None

    eod_close = _safe_float(bars[-1].get("close"))
    outcomes = {
        "return_15m_pct": pct(_first_close_at_or_after(bars, observed_at + timedelta(minutes=15))),
        "return_30m_pct": pct(_first_close_at_or_after(bars, observed_at + timedelta(minutes=30))),
        "return_60m_pct": pct(_first_close_at_or_after(bars, observed_at + timedelta(minutes=60))),
        "return_eod_pct": pct(eod_close),
    }
    return {"forward_returns_available": any(v is not None for v in outcomes.values()), **outcomes}


def _example_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "timestamp",
        "price",
        "gain_pct",
        "rel_volume",
        "spread_pct",
        "avg_volume",
        "reject_reason",
        "raw_reject_reason",
        "other_gate_would_still_block",
        "forward_returns_available",
        "return_15m_pct",
        "return_30m_pct",
        "return_60m_pct",
        "return_eod_pct",
    )
    return {key: row.get(key) for key in keys}


def _unique_symbol_examples(rows: Sequence[Mapping[str, Any]], *, limit: int = 25) -> list[dict[str, Any]]:
    seen: set[str] = set()
    examples: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol in seen:
            continue
        seen.add(symbol)
        examples.append(_example_row(row))
        if len(examples) >= limit:
            break
    return examples


def build_dynamic_rvol_sensitivity_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "paper_bot",
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Build a research-only report for hypothetical dynamic RVOL thresholds."""
    data_path = Path(data_dir)
    resolved_day = latest_dynamic_rvol_sensitivity_date(
        data_dir=data_path, user_id=user_id, history_dir=history_dir
    ) if str(day).strip().lower() == "latest" else str(day).strip()
    if not resolved_day:
        raise FileNotFoundError("No dynamic scan-history date found.")
    history_path = Path(history_dir) if history_dir is not None else data_path / "dynamic_scan_history"
    paths, source_mode = _history_paths_for_date(history_path, day=resolved_day, user_id=user_id)
    rows = _load_scan_candidates(paths, day=resolved_day)
    log_path_objs = [Path(path) for path in (log_paths or [])]
    if log_path_objs:
        rows.extend(_load_log_candidates(log_path_objs, day=resolved_day))
    bar_cache: dict[str, list[dict[str, Any]]] = {}
    enriched_rows: list[dict[str, Any]] = []
    for row in rows:
        enriched = dict(row)
        enriched["other_gate_would_still_block"] = (
            not enriched["accepted"] and enriched["reject_reason"] != "below_min_relative_volume"
        )
        enriched.update(
            _forward_returns(enriched, data_dir=data_path, bars_dir=bars_dir, day=resolved_day, cache=bar_cache)
        )
        enriched_rows.append(enriched)

    rejected = [row for row in enriched_rows if not row["accepted"]]
    rvol_only = [row for row in rejected if row["reject_reason"] == "below_min_relative_volume"]
    other_blocked = [row for row in rejected if row["reject_reason"] != "below_min_relative_volume"]
    reason_counts = Counter(str(row["reject_reason"]) for row in rejected)

    threshold_reports: dict[str, Any] = {}
    for name, threshold in _THRESHOLDS:
        would_clear = [
            row for row in rvol_only if (_safe_float(row.get("rel_volume")) is not None and float(row["rel_volume"]) >= threshold)
        ]
        threshold_reports[name] = {
            "threshold": threshold,
            "total_scanned_candidates": len(enriched_rows),
            "candidates_rejected_only_by_rvol": len(rvol_only),
            "candidates_that_would_pass_if_rvol_relaxed": len(would_clear),
            "unique_symbols_that_would_pass": len({row["symbol"] for row in would_clear}),
            "symbols": sorted({row["symbol"] for row in would_clear}),
            "examples": _unique_symbol_examples(
                sorted(would_clear, key=lambda row: (-(row.get("rel_volume") or 0), row.get("symbol") or "")),
                limit=50,
            ),
            "other_gate_still_blocks_count": len(other_blocked),
            "other_gate_examples": _unique_symbol_examples(other_blocked, limit=15),
        }

    key_examples = {
        symbol: _example_row(row)
        for symbol in ("ASTN", "NOK", "INTC", "POEL", "DSY", "AVGO", "GOOGL")
        for row in enriched_rows
        if row.get("symbol") == symbol
    }

    return {
        "report": "dynamic_rvol_sensitivity",
        "research_only": True,
        "date": resolved_day,
        "requested_user": user_id,
        "source_mode": source_mode,
        "source_files": [str(path) for path in paths],
        "log_files": [str(path) for path in log_path_objs],
        "summary": {
            "total_scanned_candidates": len(enriched_rows),
            "rejected_candidates": len(rejected),
            "accepted_candidates": len([row for row in enriched_rows if row["accepted"]]),
            "rvol_only_rejections": len(rvol_only),
            "unique_rvol_only_symbols": len({row["symbol"] for row in rvol_only}),
            "other_gate_rejections": len(other_blocked),
            "reason_counts": dict(sorted(reason_counts.items())),
            "forward_return_rows": len([row for row in enriched_rows if row.get("forward_returns_available")]),
        },
        "thresholds": threshold_reports,
        "rvol_only_examples": _unique_symbol_examples(
            sorted(rvol_only, key=lambda row: (-(row.get("rel_volume") or 0), row.get("symbol") or "")),
            limit=100,
        ),
        "key_symbol_examples": key_examples,
    }


def render_dynamic_rvol_sensitivity_report(report: Mapping[str, Any]) -> str:
    """Render a text report for humans."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    thresholds = report.get("thresholds") if isinstance(report.get("thresholds"), Mapping) else {}
    lines = [
        f"Dynamic RVOL Sensitivity - {report.get('date')} user={report.get('requested_user')}",
        "Research-only: no trading behavior, thresholds, sizing, allocator, risk, entry, or options logic changed.",
        "",
        "Summary",
        f"- source_mode: {report.get('source_mode')}",
        f"- source_files: {len(report.get('source_files') or [])}",
        f"- total scanned candidates: {summary.get('total_scanned_candidates', 0)}",
        f"- rejected candidates: {summary.get('rejected_candidates', 0)}",
        f"- accepted candidates: {summary.get('accepted_candidates', 0)}",
        f"- RVOL-only rejections: {summary.get('rvol_only_rejections', 0)}",
        f"- other-gate rejections: {summary.get('other_gate_rejections', 0)}",
        f"- forward-return rows: {summary.get('forward_return_rows', 0)}",
        "",
        "Rejection Reason Counts",
    ]
    reason_counts = summary.get("reason_counts") if isinstance(summary.get("reason_counts"), Mapping) else {}
    if reason_counts:
        for reason, count in sorted(reason_counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("Threshold Comparison")
    for name in ("current_100", "relaxed_075", "relaxed_050"):
        block = thresholds.get(name) if isinstance(thresholds.get(name), Mapping) else {}
        symbols = block.get("symbols") or []
        lines.extend(
            [
                f"- {name} threshold={block.get('threshold')}",
                f"  total_scanned_candidates={block.get('total_scanned_candidates', 0)}",
                f"  candidates_rejected_only_by_rvol={block.get('candidates_rejected_only_by_rvol', 0)}",
                f"  candidates_that_would_pass_if_rvol_relaxed={block.get('candidates_that_would_pass_if_rvol_relaxed', 0)}",
                f"  unique_symbols_that_would_pass={block.get('unique_symbols_that_would_pass', 0)}",
                f"  symbols={', '.join(symbols[:25]) if symbols else 'none'}",
            ]
        )
    lines.append("")
    lines.append("RVOL-Only Examples")
    examples = report.get("rvol_only_examples") if isinstance(report.get("rvol_only_examples"), list) else []
    if not examples:
        lines.append("- none")
    for row in examples[:25]:
        lines.append(
            "- {symbol} rel={rel_volume} gain={gain_pct}% spread={spread_pct}% "
            "price={price} avg_volume={avg_volume} reason={reject_reason} "
            "15m={return_15m_pct} 30m={return_30m_pct} 60m={return_60m_pct} eod={return_eod_pct}".format(
                **row
            )
        )
    key_examples = report.get("key_symbol_examples") if isinstance(report.get("key_symbol_examples"), Mapping) else {}
    if key_examples:
        lines.append("")
        lines.append("Requested Symbol Examples")
        for symbol in sorted(key_examples):
            row = key_examples[symbol]
            lines.append(
                "- {symbol} rel={rel_volume} gain={gain_pct}% spread={spread_pct}% "
                "reason={reject_reason} other_gate_would_still_block={other_gate_would_still_block}".format(
                    **row
                )
            )
    lines.append("")
    lines.append("Interpretation")
    rvol_count = int(summary.get("rvol_only_rejections") or 0)
    pass_075 = int((thresholds.get("relaxed_075") or {}).get("candidates_that_would_pass_if_rvol_relaxed") or 0)
    pass_050 = int((thresholds.get("relaxed_050") or {}).get("candidates_that_would_pass_if_rvol_relaxed") or 0)
    if rvol_count == 0:
        lines.append("- RVOL was not the binding observed gate in this sample.")
    elif pass_050 == 0:
        lines.append("- RVOL rejections existed, but sampled candidates were below even a 0.50 hypothetical floor.")
    else:
        lines.append(
            f"- A 0.75 hypothetical floor would clear {pass_075} RVOL-only rows; "
            f"a 0.50 floor would clear {pass_050}. Review forward returns before changing any live threshold."
        )
    return "\n".join(lines) + "\n"


def write_dynamic_rvol_sensitivity_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "paper_bot",
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write JSON/TXT artifacts for the research report."""
    data_path = Path(data_dir)
    report = build_dynamic_rvol_sensitivity_report(
        data_dir=data_path,
        day=day,
        user_id=user_id,
        history_dir=history_dir,
        bars_dir=bars_dir,
        log_paths=log_paths,
    )
    out_dir = data_path / "research" / "dynamic_rvol_sensitivity"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = _safe_user(user_id)
    stem = f"{report['date']}_{safe_user}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_dynamic_rvol_sensitivity_report(report), encoding="utf-8")
    return json_path, text_path, report
