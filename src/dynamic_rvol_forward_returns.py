"""Research-only dynamic RVOL forward-return sensitivity report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.dynamic_rvol_sensitivity import (
    _forward_returns,
    _history_paths_for_date,
    _load_log_candidates,
    _load_scan_candidates,
    _normalize_reason,
    _round,
    _safe_float,
    latest_dynamic_rvol_sensitivity_date,
)

_BUCKETS: tuple[tuple[str, float, float | None], ...] = (
    ("0.50-0.75", 0.50, 0.75),
    ("0.75-1.00", 0.75, 1.00),
    ("1.00+", 1.00, None),
)
_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("current_100", 1.00),
    ("relaxed_075", 0.75),
    ("relaxed_050", 0.50),
)
_FOCUS_SYMBOLS: tuple[str, ...] = ("ASTN", "AXTX", "INTC", "RKLZ", "RZLV", "NOK", "AAL")


@dataclass(frozen=True)
class DynamicRvolForwardReturnPaths:
    """Artifact paths written for the dynamic RVOL forward-return report."""

    json_path: Path
    text_path: Path


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _avg(values: Sequence[Any]) -> float | None:
    nums = [float(v) for v in (_safe_float(value) for value in values) if v is not None]
    return round(sum(nums) / len(nums), 4) if nums else None


def _win_rate(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [_safe_float(row.get(field)) for row in rows]
    available = [value for value in values if value is not None]
    if not available:
        return None
    wins = len([value for value in available if value > 0.0])
    return round(wins / len(available), 4)


def _return_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, field in (
        ("15m", "return_15m_pct"),
        ("30m", "return_30m_pct"),
        ("60m", "return_60m_pct"),
        ("eod", "return_eod_pct"),
    ):
        values = [row.get(field) for row in rows]
        available = [_safe_float(value) for value in values if _safe_float(value) is not None]
        out[label] = {
            "available": len(available),
            "average_return_pct": _avg(values),
            "win_rate": _win_rate(rows, field),
        }
    return out


def _bucket_for_rvol(value: Any) -> str | None:
    rvol = _safe_float(value)
    if rvol is None:
        return None
    for name, lo, hi in _BUCKETS:
        if rvol >= lo and (hi is None or rvol < hi):
            return name
    return None


def _bucket_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_bucket: dict[str, list[Mapping[str, Any]]] = {name: [] for name, _, _ in _BUCKETS}
    below_bucket: list[Mapping[str, Any]] = []
    for row in rows:
        bucket = _bucket_for_rvol(row.get("rel_volume"))
        if bucket is None:
            below_bucket.append(row)
        else:
            by_bucket[bucket].append(row)
    out: dict[str, Any] = {}
    for name, _, _ in _BUCKETS:
        bucket_rows = by_bucket[name]
        out[name] = {
            "candidate_count": len(bucket_rows),
            "unique_symbol_count": len({str(row.get("symbol") or "").upper() for row in bucket_rows}),
            "average_rvol": _avg([row.get("rel_volume") for row in bucket_rows]),
            "average_gain_at_rejection_pct": _avg([row.get("gain_pct") for row in bucket_rows]),
            "average_spread_pct": _avg([row.get("spread_pct") for row in bucket_rows]),
            "forward_returns": _return_stats(bucket_rows),
            "symbols": sorted({str(row.get("symbol") or "").upper() for row in bucket_rows if row.get("symbol")}),
        }
    out["below_0.50_or_missing"] = {
        "candidate_count": len(below_bucket),
        "unique_symbol_count": len({str(row.get("symbol") or "").upper() for row in below_bucket}),
        "average_rvol": _avg([row.get("rel_volume") for row in below_bucket]),
    }
    return out


def _threshold_stats(rows: Sequence[Mapping[str, Any]], *, threshold: float) -> dict[str, Any]:
    admitted = [row for row in rows if (_safe_float(row.get("rel_volume")) is not None and float(row["rel_volume"]) >= threshold)]
    return {
        "threshold": threshold,
        "additional_candidates_admitted": len(admitted),
        "unique_symbols_admitted": len({str(row.get("symbol") or "").upper() for row in admitted}),
        "average_rvol": _avg([row.get("rel_volume") for row in admitted]),
        "average_gain_at_rejection_pct": _avg([row.get("gain_pct") for row in admitted]),
        "average_spread_pct": _avg([row.get("spread_pct") for row in admitted]),
        "forward_returns": _return_stats(admitted),
        "symbols": sorted({str(row.get("symbol") or "").upper() for row in admitted if row.get("symbol")}),
    }


def _field_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"true", "t", "1", "yes"}:
        return True
    if text in {"false", "f", "0", "no"}:
        return False
    return None


def _other_major_gates_acceptable(row: Mapping[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    raw_reason = str(row.get("raw_reject_reason") or row.get("reject_reason") or "").lower()
    if "unstable_quote" in raw_reason or "unstable quote" in raw_reason:
        blockers.append("unstable_quote")
    if "bad_quote" in raw_reason or "bad quote" in raw_reason:
        blockers.append("bad_quote")
    for field, blocker in (
        ("price_ok", "price"),
        ("spread_ok", "spread"),
        ("avg_volume_ok", "avg_volume"),
        ("gain_ok", "gain"),
        ("quote_stable", "unstable_quote"),
        ("bad_quote_ok", "bad_quote"),
    ):
        flag = _field_bool(row.get(field))
        if flag is False:
            blockers.append(blocker)
    price = _safe_float(row.get("price"))
    min_price = _safe_float(row.get("min_price"))
    if price is not None and min_price is not None and price < min_price:
        blockers.append("price")
    spread = _safe_float(row.get("spread_pct"))
    max_spread = _safe_float(row.get("max_spread_pct"))
    if spread is not None and max_spread is not None and spread > max_spread:
        blockers.append("spread")
    avg_volume = _safe_float(row.get("avg_volume"))
    min_avg_volume = _safe_float(row.get("min_avg_volume"))
    if avg_volume is not None and min_avg_volume is not None and avg_volume < min_avg_volume:
        blockers.append("avg_volume")
    gain = _safe_float(row.get("gain_pct"))
    min_gain = _safe_float(row.get("min_gain_pct"))
    if gain is not None and min_gain is not None and gain < min_gain:
        blockers.append("gain")
    return not blockers, sorted(set(blockers))


def _load_rejection_report_rows(data_dir: Path, *, day: str, user_id: str) -> list[dict[str, Any]]:
    roots = [
        data_dir / "research" / "dynamic_rejections",
        data_dir / "research_feedback",
        Path("reports") / "research_feedback",
    ]
    safe_user = _safe_user(user_id)
    rows: list[dict[str, Any]] = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.glob(f"*{day}*{safe_user}*.json")) + sorted(root.glob(f"*{day}*default*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            raw_rows = payload.get("rows") or payload.get("rejected") or payload.get("rejected_candidates") or []
            if not isinstance(raw_rows, list):
                continue
            for index, raw in enumerate(raw_rows, start=1):
                if not isinstance(raw, Mapping):
                    continue
                reason = raw.get("reject_reason") or raw.get("rejection_reason") or raw.get("reason")
                row = {
                    "symbol": str(raw.get("symbol") or "").strip().upper(),
                    "timestamp": raw.get("timestamp"),
                    "accepted": False,
                    "price": _round(raw.get("price")),
                    "gain_pct": _round(raw.get("gain_pct", raw.get("day_gain_pct"))),
                    "rel_volume": _round(raw.get("rel_volume", raw.get("relative_volume"))),
                    "spread_pct": _round(raw.get("spread_pct")),
                    "avg_volume": _round(raw.get("avg_volume"), 2),
                    "reject_reason": _normalize_reason(reason),
                    "raw_reject_reason": str(reason or "").strip() or None,
                    "source_file": str(path),
                    "source_sequence": index,
                    "source_date": day,
                    "price_ok": raw.get("price_ok"),
                    "spread_ok": raw.get("spread_ok"),
                    "avg_volume_ok": raw.get("avg_volume_ok"),
                    "gain_ok": raw.get("gain_ok"),
                    "quote_stable": raw.get("quote_stable"),
                    "bad_quote_ok": raw.get("bad_quote_ok"),
                    "min_price": raw.get("min_price"),
                    "max_spread_pct": raw.get("max_spread_pct"),
                    "min_avg_volume": raw.get("min_avg_volume"),
                    "min_gain_pct": raw.get("min_gain_pct"),
                }
                if row["symbol"]:
                    rows.append(row)
    return rows


def _example_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row.get("symbol"),
        "timestamp": row.get("timestamp"),
        "price": row.get("price"),
        "gain_pct": row.get("gain_pct"),
        "rel_volume": row.get("rel_volume"),
        "spread_pct": row.get("spread_pct"),
        "avg_volume": row.get("avg_volume"),
        "bucket": row.get("rvol_bucket"),
        "forward_returns_available": row.get("forward_returns_available"),
        "return_15m_pct": row.get("return_15m_pct"),
        "return_30m_pct": row.get("return_30m_pct"),
        "return_60m_pct": row.get("return_60m_pct"),
        "return_eod_pct": row.get("return_eod_pct"),
        "source_file": row.get("source_file"),
    }


def _per_symbol(rows: Sequence[Mapping[str, Any]], symbols: Sequence[str]) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("symbol") or "").upper()].append(row)
    out: dict[str, Any] = {}
    for symbol in symbols:
        sym = symbol.upper()
        symbol_rows = grouped.get(sym, [])
        out[sym] = {
            "candidate_count": len(symbol_rows),
            "average_rvol": _avg([row.get("rel_volume") for row in symbol_rows]),
            "average_gain_at_rejection_pct": _avg([row.get("gain_pct") for row in symbol_rows]),
            "average_spread_pct": _avg([row.get("spread_pct") for row in symbol_rows]),
            "forward_returns": _return_stats(symbol_rows),
            "examples": [_example_row(row) for row in symbol_rows[:10]],
        }
    return out


def _alignment_forward_return_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "candidate_count": len(rows),
        "unique_symbol_count": len({str(row.get("symbol") or "").upper() for row in rows}),
        "missing_forward_return_rows": len([row for row in rows if not row.get("forward_returns_available")]),
        "average_rvol": _avg([row.get("rel_volume") for row in rows]),
        "average_gain_at_rejection_pct": _avg([row.get("gain_pct") for row in rows]),
        "average_spread_pct": _avg([row.get("spread_pct") for row in rows]),
        "forward_returns": _return_stats(rows),
        "symbols": sorted({str(row.get("symbol") or "").upper() for row in rows if row.get("symbol")}),
        "examples": [_example_row(row) for row in rows[:100]],
    }


def _quality_interpretation(thresholds: Mapping[str, Any]) -> str:
    def eod(name: str) -> float | None:
        block = thresholds.get(name)
        if not isinstance(block, Mapping):
            return None
        returns = block.get("forward_returns")
        if not isinstance(returns, Mapping):
            return None
        eod_block = returns.get("eod")
        if not isinstance(eod_block, Mapping):
            return None
        return _safe_float(eod_block.get("average_return_pct"))

    base = eod("current_100")
    relaxed_075 = eod("relaxed_075")
    relaxed_050 = eod("relaxed_050")
    available = [value for value in (base, relaxed_075, relaxed_050) if value is not None]
    if not available:
        return "insufficient_forward_return_data"
    if base is None:
        return "current_threshold_has_no_forward_return_sample"
    worsens = [value for value in (relaxed_075, relaxed_050) if value is not None and value < base]
    improves = [value for value in (relaxed_075, relaxed_050) if value is not None and value > base]
    if worsens and not improves:
        return "lowering_rvol_worsens_average_eod_quality_in_sample"
    if improves and not worsens:
        return "lowering_rvol_improves_average_eod_quality_in_sample"
    return "mixed_threshold_quality_in_sample"


def build_dynamic_rvol_forward_returns_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "paper_bot",
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
    focus_symbols: Sequence[str] = _FOCUS_SYMBOLS,
) -> dict[str, Any]:
    """Build a research-only report for RVOL-rejected candidates and later returns."""
    data_path = Path(data_dir)
    resolved_day = (
        latest_dynamic_rvol_sensitivity_date(data_dir=data_path, user_id=user_id, history_dir=history_dir)
        if str(day).strip().lower() == "latest"
        else str(day).strip()
    )
    if not resolved_day:
        raise FileNotFoundError("No dynamic scan-history date found.")
    history_path = Path(history_dir) if history_dir is not None else data_path / "dynamic_scan_history"
    paths, source_mode = _history_paths_for_date(history_path, day=resolved_day, user_id=user_id)
    rows = _load_scan_candidates(paths, day=resolved_day)
    log_path_objs = [Path(path) for path in (log_paths or [])]
    if log_path_objs:
        rows.extend(_load_log_candidates(log_path_objs, day=resolved_day))
    rows.extend(_load_rejection_report_rows(data_path, day=resolved_day, user_id=user_id))

    rvol_rejected: list[dict[str, Any]] = []
    alignment_rejected: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    bar_cache: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if bool(row.get("accepted")):
            continue
        reject_reason = str(row.get("reject_reason") or "")
        if reject_reason == "entry_alignment":
            enriched = dict(row)
            enriched["other_major_gates_acceptable"] = True
            enriched["excluded_other_gate_blockers"] = []
            enriched.update(
                _forward_returns(enriched, data_dir=data_path, bars_dir=bars_dir, day=resolved_day, cache=bar_cache)
            )
            alignment_rejected.append(enriched)
            continue
        if reject_reason == "below_min_relative_volume":
            gates_ok, blockers = _other_major_gates_acceptable(row)
            if not gates_ok:
                for blocker in blockers:
                    excluded[blocker] += 1
                continue
        else:
            continue
        enriched = dict(row)
        enriched["rvol_bucket"] = _bucket_for_rvol(enriched.get("rel_volume"))
        enriched["other_major_gates_acceptable"] = True
        enriched["excluded_other_gate_blockers"] = []
        enriched.update(
            _forward_returns(enriched, data_dir=data_path, bars_dir=bars_dir, day=resolved_day, cache=bar_cache)
        )
        rvol_rejected.append(enriched)

    bucket_analysis = _bucket_stats(rvol_rejected)
    threshold_analysis = {
        name: _threshold_stats(rvol_rejected, threshold=threshold)
        for name, threshold in _THRESHOLDS
    }
    reason_counts = Counter(str(row.get("reject_reason") or "unknown") for row in rows if not bool(row.get("accepted")))
    interpretation = _quality_interpretation(threshold_analysis)
    return {
        "report": "dynamic_rvol_forward_returns",
        "research_only": True,
        "date": resolved_day,
        "requested_user": user_id,
        "source_mode": source_mode,
        "source_files": [str(path) for path in paths],
        "log_files": [str(path) for path in log_path_objs],
        "summary": {
            "total_candidates_loaded": len(rows),
            "rvol_rejections_considered": len(rvol_rejected),
            "unique_symbols_considered": len({str(row.get("symbol") or "").upper() for row in rvol_rejected}),
            "missing_forward_return_rows": len([row for row in rvol_rejected if not row.get("forward_returns_available")]),
            "excluded_other_gate_blockers": dict(sorted(excluded.items())),
            "rejection_reason_counts": dict(sorted(reason_counts.items())),
            "quality_interpretation": interpretation,
        },
        "bucket_analysis": bucket_analysis,
        "threshold_analysis": threshold_analysis,
        "entry_alignment_forward_returns": _alignment_forward_return_stats(alignment_rejected),
        "per_symbol_analysis": _per_symbol(rvol_rejected, focus_symbols),
        "examples": [_example_row(row) for row in rvol_rejected[:100]],
    }


def render_dynamic_rvol_forward_returns_report(report: Mapping[str, Any]) -> str:
    """Render a concise text version of the RVOL forward-return report."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    lines = [
        f"Dynamic RVOL Forward Returns - {report.get('date')} user={report.get('requested_user')}",
        "Research-only: no trading behavior, scanner thresholds, config, risk, allocator, or sizing logic changed.",
        "",
        "Summary",
        f"- source_mode: {report.get('source_mode')}",
        f"- total_candidates_loaded: {summary.get('total_candidates_loaded', 0)}",
        f"- rvol_rejections_considered: {summary.get('rvol_rejections_considered', 0)}",
        f"- unique_symbols_considered: {summary.get('unique_symbols_considered', 0)}",
        f"- missing_forward_return_rows: {summary.get('missing_forward_return_rows', 0)}",
        f"- excluded_other_gate_blockers: {summary.get('excluded_other_gate_blockers', {})}",
        f"- quality_interpretation: {summary.get('quality_interpretation')}",
        "",
        "RVOL Buckets",
    ]
    buckets = report.get("bucket_analysis") if isinstance(report.get("bucket_analysis"), Mapping) else {}
    for name in ("0.50-0.75", "0.75-1.00", "1.00+"):
        block = buckets.get(name) if isinstance(buckets.get(name), Mapping) else {}
        eod = ((block.get("forward_returns") or {}).get("eod") or {}) if isinstance(block.get("forward_returns"), Mapping) else {}
        lines.append(
            "- {name}: count={count} unique={unique} avg_rvol={rvol} avg_gain={gain} "
            "avg_spread={spread} eod_avg={eod_avg} eod_win_rate={win}".format(
                name=name,
                count=block.get("candidate_count", 0),
                unique=block.get("unique_symbol_count", 0),
                rvol=block.get("average_rvol"),
                gain=block.get("average_gain_at_rejection_pct"),
                spread=block.get("average_spread_pct"),
                eod_avg=eod.get("average_return_pct"),
                win=eod.get("win_rate"),
            )
        )
    lines.append("")
    lines.append("Threshold Comparison")
    thresholds = report.get("threshold_analysis") if isinstance(report.get("threshold_analysis"), Mapping) else {}
    for name in ("current_100", "relaxed_075", "relaxed_050"):
        block = thresholds.get(name) if isinstance(thresholds.get(name), Mapping) else {}
        eod = ((block.get("forward_returns") or {}).get("eod") or {}) if isinstance(block.get("forward_returns"), Mapping) else {}
        lines.append(
            "- {name}: threshold={threshold} additional={additional} unique={unique} "
            "eod_avg={eod_avg} eod_win_rate={win} symbols={symbols}".format(
                name=name,
                threshold=block.get("threshold"),
                additional=block.get("additional_candidates_admitted", 0),
                unique=block.get("unique_symbols_admitted", 0),
                eod_avg=eod.get("average_return_pct"),
                win=eod.get("win_rate"),
                symbols=", ".join((block.get("symbols") or [])[:25]) or "none",
            )
        )
    lines.append("")
    alignment = report.get("entry_alignment_forward_returns")
    alignment = alignment if isinstance(alignment, Mapping) else {}
    alignment_eod = (
        ((alignment.get("forward_returns") or {}).get("eod") or {})
        if isinstance(alignment.get("forward_returns"), Mapping)
        else {}
    )
    lines.append("Entry Alignment Rejects")
    lines.append(
        "- count={count} unique={unique} missing_forward_returns={missing} "
        "eod_avg={eod_avg} eod_win_rate={win} symbols={symbols}".format(
            count=alignment.get("candidate_count", 0),
            unique=alignment.get("unique_symbol_count", 0),
            missing=alignment.get("missing_forward_return_rows", 0),
            eod_avg=alignment_eod.get("average_return_pct"),
            win=alignment_eod.get("win_rate"),
            symbols=", ".join((alignment.get("symbols") or [])[:25]) or "none",
        )
    )
    lines.append("")
    lines.append("Focus Symbols")
    per_symbol = report.get("per_symbol_analysis") if isinstance(report.get("per_symbol_analysis"), Mapping) else {}
    for symbol in _FOCUS_SYMBOLS:
        block = per_symbol.get(symbol) if isinstance(per_symbol.get(symbol), Mapping) else {}
        eod = ((block.get("forward_returns") or {}).get("eod") or {}) if isinstance(block.get("forward_returns"), Mapping) else {}
        lines.append(
            f"- {symbol}: count={block.get('candidate_count', 0)} avg_rvol={block.get('average_rvol')} "
            f"avg_gain={block.get('average_gain_at_rejection_pct')} eod_avg={eod.get('average_return_pct')} "
            f"eod_win_rate={eod.get('win_rate')}"
        )
    lines.append("")
    lines.append("Interpretation")
    lines.append(
        "- Forward-return evidence is diagnostic only; threshold changes require separate scanner/risk review."
    )
    return "\n".join(lines) + "\n"


def dynamic_rvol_forward_return_paths(
    *,
    data_dir: Path | str,
    user_id: str,
    day: str,
) -> DynamicRvolForwardReturnPaths:
    root = Path(data_dir) / "research" / "dynamic_rvol_forward_returns"
    stem = f"{day}_{_safe_user(user_id)}"
    return DynamicRvolForwardReturnPaths(json_path=root / f"{stem}.json", text_path=root / f"{stem}.txt")


def write_dynamic_rvol_forward_returns_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "paper_bot",
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
    log_paths: Sequence[Path | str] | None = None,
) -> tuple[Path, Path, dict[str, Any]]:
    """Build and write JSON/TXT artifacts for the research report."""
    report = build_dynamic_rvol_forward_returns_report(
        data_dir=data_dir,
        day=day,
        user_id=user_id,
        history_dir=history_dir,
        bars_dir=bars_dir,
        log_paths=log_paths,
    )
    paths = dynamic_rvol_forward_return_paths(data_dir=data_dir, user_id=user_id, day=str(report["date"]))
    paths.json_path.parent.mkdir(parents=True, exist_ok=True)
    paths.json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    paths.text_path.write_text(render_dynamic_rvol_forward_returns_report(report), encoding="utf-8")
    return paths.json_path, paths.text_path, report


__all__ = [
    "build_dynamic_rvol_forward_returns_report",
    "render_dynamic_rvol_forward_returns_report",
    "write_dynamic_rvol_forward_returns_report",
    "dynamic_rvol_forward_return_paths",
]
