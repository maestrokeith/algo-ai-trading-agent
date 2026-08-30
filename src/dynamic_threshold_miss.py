"""Research-only report for near-threshold dynamic scanner misses."""

from __future__ import annotations

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
_SCAN_RE = re.compile(r"\bDYNAMIC_SCAN\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<body>.+)$")
_REJECT_RE = re.compile(r"\bDYNAMIC_SCAN reject\s+(?P<symbol>[A-Z][A-Z0-9.\-]{0,9}):\s+(?P<body>.+)$")
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
_DEFAULT_THRESHOLDS = {
    "min_relative_volume": 1.0,
    "min_day_gain_pct": 3.0,
    "max_spread_pct": 5.0,
    "min_avg_volume": 10_000.0,
}


@dataclass(frozen=True)
class DynamicThresholdMissPaths:
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
    return dt.astimezone(_ET)


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


def _parse_kv(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2).rstrip(",;%") for match in _KV_RE.finditer(text)}


def _normalize_reason(reason: Any) -> str:
    text = str(reason or "").strip().lower().replace(" ", "_").replace("-", "_")
    if "relative_volume" in text or "rel_volume" in text:
        return "below_min_relative_volume"
    if "day_gain" in text or "min_gain" in text:
        return "below_min_day_gain"
    if "avg_volume" in text:
        return "below_min_avg_volume"
    if "spread" in text:
        return "spread_too_wide"
    if "gain_filter" in text:
        return "gain_filter"
    if "price" in text:
        return "below_min_price"
    if "unstable" in text and "quote" in text:
        return "unstable_quote"
    if "atr" in text:
        return "atr_expansion"
    if "entry_alignment" in text:
        return "entry_alignment"
    return text or "unknown"


def _threshold_from_reason(reason: Any, key: str, default: float) -> float:
    text = str(reason or "").lower()
    kv = _parse_kv(str(reason or ""))
    if key == "rel_volume":
        if "rel" in text or "relative_volume" in text:
            return _safe_float(kv.get("min") or kv.get("required_relative_volume")) or default
        return default
    if key == "gain_pct":
        if "gain" in text or "day_gain" in text:
            return _safe_float(kv.get("min") or kv.get("min_day_gain_pct")) or default
        return default
    if key == "spread_pct":
        if "spread" in text:
            return _safe_float(kv.get("max") or kv.get("max_spread_pct")) or default
        return default
    if key == "avg_volume":
        if "avg" in text or "avg_volume" in text or "average_volume" in text:
            return _safe_float(kv.get("min") or kv.get("min_avg_volume")) or default
        return default
    return default


def _gap(value: Any, threshold: float, *, direction: str) -> float | None:
    number = _safe_float(value)
    if number is None or threshold <= 0:
        return None
    raw = threshold - number if direction == "min" else number - threshold
    return round(raw / threshold, 4)


def _distance_class(gaps: Mapping[str, Any]) -> str:
    misses = [float(value) for value in gaps.values() if _safe_float(value) is not None and float(value) > 0]
    if not misses:
        return "cleared_thresholds"
    worst = max(misses)
    if worst < 0.20:
        return "near_miss"
    if worst < 0.50:
        return "moderate_miss"
    return "severe_miss"


def _history_paths(data_dir: Path, *, day: str, user_id: str) -> tuple[list[Path], str]:
    root = data_dir / "dynamic_scan_history"
    if not root.exists():
        return [], "none"
    safe = _safe_user(user_id)
    exact: list[Path] = []
    fallback: list[Path] = []
    for path in sorted(root.glob("*.json")):
        if _date_from_path(path) != day:
            continue
        if path.name.endswith(f"_{safe}.json"):
            exact.append(path)
        elif path.name.endswith("_default.json"):
            fallback.append(path)
    if exact:
        return exact, "exact_user"
    return fallback, "default_fallback" if fallback else "none"


def _candidate_timestamp(raw: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    ts = _parse_timestamp(raw.get("timestamp")) or _parse_timestamp(payload.get("generated_at"))
    return ts.isoformat() if ts else None


def _history_rows(paths: Sequence[Path], *, day: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (_parse_timestamp(payload.get("generated_at")) or datetime.min.replace(tzinfo=_ET)).astimezone(_ET).date().isoformat() != day:
            continue
        for idx, raw in enumerate(payload.get("candidates") or [], start=1):
            if not isinstance(raw, Mapping) or bool(raw.get("accepted")):
                continue
            quality = raw.get("quality") if isinstance(raw.get("quality"), Mapping) else {}
            reason = raw.get("rejection_reason") or quality.get("rejection_reason")
            rel = _safe_float(raw.get("rel_volume"))
            if rel is None:
                rel = _safe_float(raw.get("relative_volume"))
            rows.append(
                {
                    "symbol": str(raw.get("symbol") or "").strip().upper(),
                    "timestamp": _candidate_timestamp(raw, payload),
                    "price": _round(raw.get("price")),
                    "gain_pct": _round(raw.get("gain_pct", raw.get("day_gain_pct"))),
                    "rel_volume": _round(rel),
                    "spread_pct": _round(raw.get("spread_pct")),
                    "avg_volume": _round(raw.get("avg_volume"), 2),
                    "reject_reason": _normalize_reason(reason),
                    "raw_reject_reason": str(reason or "").strip() or None,
                    "source_file": str(path),
                    "source_sequence": idx,
                }
            )
    return [row for row in rows if row.get("symbol")]


def _log_rows(paths: Sequence[Path], *, day: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            continue
        pending: dict[str, dict[str, Any]] = {}
        for line_no, line in enumerate(_read_text(path).splitlines(), start=1):
            ts = _line_timestamp(line, day=day)
            scan = _SCAN_RE.search(line)
            if scan:
                kv = _parse_kv(scan.group("body"))
                symbol = scan.group("symbol").upper()
                pending[symbol] = {
                    "symbol": symbol,
                    "timestamp": ts,
                    "price": _round(kv.get("price")),
                    "gain_pct": _round(kv.get("gain")),
                    "rel_volume": _round(kv.get("rel") or kv.get("relative_volume")),
                    "spread_pct": _round(kv.get("spread") or kv.get("spread_pct")),
                    "avg_volume": _round(kv.get("avg"), 2),
                    "source_file": str(path),
                    "source_sequence": line_no,
                }
                continue
            reject = _REJECT_RE.search(line)
            if reject:
                symbol = reject.group("symbol").upper()
                row = dict(pending.get(symbol, {}))
                row.update(
                    {
                        "symbol": symbol,
                        "timestamp": row.get("timestamp") or ts,
                        "reject_reason": _normalize_reason(reject.group("body")),
                        "raw_reject_reason": reject.group("body").strip(),
                        "source_file": str(path),
                        "source_sequence": line_no,
                    }
                )
                kv = _parse_kv(reject.group("body"))
                row["rel_volume"] = row.get("rel_volume") if row.get("rel_volume") is not None else _round(kv.get("rel"))
                row["gain_pct"] = row.get("gain_pct") if row.get("gain_pct") is not None else _round(kv.get("gain"))
                rows.append(row)
    return [row for row in rows if row.get("symbol")]


def _enrich_row(row: Mapping[str, Any], *, data_dir: Path, bars_dir: Path | str | None, day: str) -> dict[str, Any]:
    raw_reason = row.get("raw_reject_reason")
    thresholds = {
        "rel_volume": _threshold_from_reason(raw_reason, "rel_volume", _DEFAULT_THRESHOLDS["min_relative_volume"]),
        "gain_pct": _threshold_from_reason(raw_reason, "gain_pct", _DEFAULT_THRESHOLDS["min_day_gain_pct"]),
        "spread_pct": _threshold_from_reason(raw_reason, "spread_pct", _DEFAULT_THRESHOLDS["max_spread_pct"]),
        "avg_volume": _threshold_from_reason(raw_reason, "avg_volume", _DEFAULT_THRESHOLDS["min_avg_volume"]),
    }
    gaps = {
        "rel_volume_gap": _gap(row.get("rel_volume"), thresholds["rel_volume"], direction="min"),
        "gain_gap": _gap(row.get("gain_pct"), thresholds["gain_pct"], direction="min"),
        "spread_gap": _gap(row.get("spread_pct"), thresholds["spread_pct"], direction="max"),
        "avg_volume_gap": _gap(row.get("avg_volume"), thresholds["avg_volume"], direction="min"),
    }
    rejected_at = _parse_timestamp(row.get("timestamp"))
    rejected_price = _safe_float(row.get("price"))
    bars, bar_diag = _load_local_bars_with_diagnostics(
        data_dir=data_dir,
        bars_dir=bars_dir,
        symbol=str(row.get("symbol") or ""),
        day=day,
    )
    outcome = _outcome_from_bars(bars, rejected_at=rejected_at, rejected_price=rejected_price, day=day)
    return {
        **row,
        **gaps,
        "thresholds": thresholds,
        "distance_class": _distance_class(gaps),
        "max_gain_after_rejection_pct": outcome.get("max_gain_after_block_pct")
        if outcome.get("max_gain_after_block_pct") is not None
        else outcome.get("max_gain_after_rejection_pct"),
        "return_after_15m_pct": outcome.get("return_after_15m_pct"),
        "return_after_30m_pct": outcome.get("return_after_30m_pct"),
        "return_after_60m_pct": outcome.get("return_after_60m_pct"),
        "outcome_available": outcome.get("outcome_available"),
        "missing_reason": outcome.get("missing_reason"),
        "bar_diagnostics": bar_diag,
    }


def _mean(values: Sequence[float]) -> float | None:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    return round(sum(finite) / len(finite), 4) if finite else None


def _numbers(rows: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = _safe_float(row.get(key))
        if value is not None:
            out.append(value)
    return out


def _group_summary(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "unknown")].append(row)
    return {
        name: {
            "count": len(items),
            "outcomes_available": sum(1 for row in items if row.get("outcome_available")),
            "average_max_gain_after_rejection_pct": _mean(_numbers(items, "max_gain_after_rejection_pct")),
            "average_return_after_15m_pct": _mean(_numbers(items, "return_after_15m_pct")),
            "average_return_after_30m_pct": _mean(_numbers(items, "return_after_30m_pct")),
            "average_return_after_60m_pct": _mean(_numbers(items, "return_after_60m_pct")),
        }
        for name, items in sorted(grouped.items())
    }


def _example(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "timestamp",
        "reject_reason",
        "distance_class",
        "price",
        "gain_pct",
        "rel_volume",
        "spread_pct",
        "avg_volume",
        "rel_volume_gap",
        "gain_gap",
        "spread_gap",
        "avg_volume_gap",
        "max_gain_after_rejection_pct",
        "return_after_15m_pct",
        "return_after_30m_pct",
        "return_after_60m_pct",
        "raw_reject_reason",
    )
    return {key: row.get(key) for key in keys}


def build_dynamic_threshold_miss_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    data_path = Path(data_dir)
    paths, source_mode = _history_paths(data_path, day=day, user_id=user_id)
    rows = _history_rows(paths, day=day)
    log_path_objs = [Path(path) for path in (log_paths or [])]
    if log_path_objs:
        rows.extend(_log_rows(log_path_objs, day=day))
    enriched = [_enrich_row(row, data_dir=data_path, bars_dir=bars_dir, day=day) for row in rows]
    near = [row for row in enriched if row.get("distance_class") == "near_miss"]
    profitable = sorted(
        [row for row in near if (_safe_float(row.get("max_gain_after_rejection_pct")) or 0.0) > 0],
        key=lambda row: float(row.get("max_gain_after_rejection_pct") or 0.0),
        reverse=True,
    )
    unprofitable = sorted(
        [row for row in near if (_safe_float(row.get("return_after_15m_pct")) or 0.0) <= 0],
        key=lambda row: float(row.get("return_after_15m_pct") or 0.0),
    )
    return {
        "report": "dynamic_threshold_miss_research",
        "research_only": True,
        "date": day,
        "user": user_id,
        "source_mode": source_mode,
        "source_files": [str(path) for path in paths],
        "log_files": [str(path) for path in log_path_objs],
        "threshold_defaults": _DEFAULT_THRESHOLDS,
        "summary": {
            "rejected_candidates": len(enriched),
            "outcomes_available": sum(1 for row in enriched if row.get("outcome_available")),
            "distance_class_counts": dict(Counter(str(row.get("distance_class")) for row in enriched)),
            "rejection_type_counts": dict(Counter(str(row.get("reject_reason")) for row in enriched)),
        },
        "top_profitable_near_misses": [_example(row) for row in profitable[:25]],
        "top_unprofitable_near_misses": [_example(row) for row in unprofitable[:25]],
        "outcomes_by_rejection_type": _group_summary(enriched, "reject_reason"),
        "outcomes_by_threshold_distance": _group_summary(enriched, "distance_class"),
        "candidates": [_example(row) for row in enriched],
    }


def render_dynamic_threshold_miss_report(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Dynamic Threshold Miss Research - {report.get('date')} user={report.get('user')}",
        "Research-only: no trading behavior, thresholds, sizing, allocator, risk, entry, or options logic changed.",
        "",
        "Summary",
        f"- source_mode: {report.get('source_mode')}",
        f"- source_files: {len(report.get('source_files') or [])}",
        f"- log_files: {len(report.get('log_files') or [])}",
        f"- rejected candidates: {summary.get('rejected_candidates', 0)}",
        f"- outcomes available: {summary.get('outcomes_available', 0)}",
        f"- distance classes: {summary.get('distance_class_counts')}",
        f"- rejection types: {summary.get('rejection_type_counts')}",
        "",
        "Top Profitable Near Misses",
    ]
    for row in (report.get("top_profitable_near_misses") or [])[:15]:
        lines.append(
            "- {symbol} reason={reject_reason} class={distance_class} gain={gain_pct} rel={rel_volume} "
            "max_gain={max_gain_after_rejection_pct} r15={return_after_15m_pct} r30={return_after_30m_pct} r60={return_after_60m_pct}".format(
                **row
            )
        )
    if not report.get("top_profitable_near_misses"):
        lines.append("- none")
    lines.append("")
    lines.append("Top Unprofitable Near Misses")
    for row in (report.get("top_unprofitable_near_misses") or [])[:15]:
        lines.append(
            "- {symbol} reason={reject_reason} class={distance_class} gain={gain_pct} rel={rel_volume} "
            "max_gain={max_gain_after_rejection_pct} r15={return_after_15m_pct} r30={return_after_30m_pct} r60={return_after_60m_pct}".format(
                **row
            )
        )
    if not report.get("top_unprofitable_near_misses"):
        lines.append("- none")
    lines.append("")
    lines.append("Average Outcomes by Rejection Type")
    for key, block in sorted((report.get("outcomes_by_rejection_type") or {}).items()):
        lines.append(f"- {key}: {block}")
    lines.append("")
    lines.append("Average Outcomes by Threshold Distance")
    for key, block in sorted((report.get("outcomes_by_threshold_distance") or {}).items()):
        lines.append(f"- {key}: {block}")
    return "\n".join(lines) + "\n"


def write_dynamic_threshold_miss_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    data_path = Path(data_dir)
    report = build_dynamic_threshold_miss_report(
        data_dir=data_path,
        day=day,
        user_id=user_id,
        bars_dir=bars_dir,
        log_paths=log_paths,
    )
    out_dir = data_path / "research" / "dynamic_threshold_miss"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{day}_{_safe_user(user_id)}"
    json_path = out_dir / f"{stem}.json"
    text_path = out_dir / f"{stem}.txt"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_dynamic_threshold_miss_report(report), encoding="utf-8")
    return json_path, text_path, report
