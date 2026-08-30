"""Research-only unstable quote rejection analysis for dynamic scans."""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.dynamic_rvol_sensitivity import (
    _forward_returns,
    _history_paths_for_date,
    _normalize_reason,
    _parse_timestamp,
    _round,
    _safe_float,
    latest_dynamic_rvol_sensitivity_date,
)

_ET = ZoneInfo("America/New_York")


def _safe_user(user_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(user_id or "default")) or "default"


def _market_open(day: str) -> datetime:
    return datetime.combine(datetime.fromisoformat(day).date(), time(hour=9, minute=30), tzinfo=_ET)


def _first_window_bounds(day: str, minutes: int) -> tuple[datetime, datetime]:
    start = _market_open(day)
    return start, start + timedelta(minutes=int(minutes))


def _candidate_timestamp(raw: Mapping[str, Any], payload: Mapping[str, Any]) -> str | None:
    value = raw.get("timestamp") or raw.get("observed_at") or payload.get("generated_at")
    text = str(value or "").strip()
    return text or None


def _row_in_window(timestamp: str | None, *, day: str, minutes: int) -> bool:
    ts = _parse_timestamp(timestamp)
    if ts is None:
        return False
    start, end = _first_window_bounds(day, minutes)
    local = ts.astimezone(_ET)
    return start <= local < end


def _candidate_row(raw: Mapping[str, Any], *, payload: Mapping[str, Any], path: Path, sequence: int) -> dict[str, Any]:
    quality = raw.get("quality") if isinstance(raw.get("quality"), Mapping) else {}
    reason = raw.get("rejection_reason") or raw.get("reason") or quality.get("rejection_reason")
    symbol = str(raw.get("symbol") or "").strip().upper()
    price = _safe_float(raw.get("price"))
    spread_pct = _safe_float(raw.get("spread_pct"))
    bid = _safe_float(raw.get("bid") or raw.get("bid_price"))
    ask = _safe_float(raw.get("ask") or raw.get("ask_price"))
    spread_dollars = None
    if bid is not None and ask is not None and ask >= bid:
        spread_dollars = round(ask - bid, 6)
    return {
        "symbol": symbol,
        "timestamp": _candidate_timestamp(raw, payload),
        "accepted": bool(raw.get("accepted")),
        "price": _round(price),
        "bid": _round(bid),
        "ask": _round(ask),
        "bid_ask_spread_dollars": _round(spread_dollars, 6),
        "bid_ask_spread_pct": _round(spread_pct),
        "quote_age_seconds": _round(raw.get("quote_age_seconds") or raw.get("quote_age_sec") or raw.get("age_seconds")),
        "quote_variance": None,
        "gain_pct": _round(raw.get("gain_pct", raw.get("day_gain_pct"))),
        "rel_volume": _round(raw.get("rel_volume", raw.get("relative_volume"))),
        "avg_volume": _round(raw.get("avg_volume"), 2),
        "volume": _round(raw.get("volume"), 2),
        "reject_reason": _normalize_reason(reason),
        "raw_reject_reason": str(reason or "").strip() or None,
        "news_score": _round(raw.get("news_score"), 2),
        "event_score": _round(raw.get("event_score"), 2),
        "catalyst_score": _round(raw.get("catalyst_score"), 4),
        "source_file": str(path),
        "source_sequence": sequence,
    }


def _load_scan_rows(paths: Sequence[Path], *, day: str, window_minutes: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sequence = 0
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, Mapping):
            continue
        generated_at = _parse_timestamp(payload.get("generated_at"))
        if str(payload.get("generated_at") or "")[:10] and generated_at is not None:
            generated_day = generated_at.astimezone(_ET).date().isoformat()
            if generated_day != day:
                continue
        candidates = payload.get("candidates")
        if not isinstance(candidates, list):
            candidates = (payload.get("rejected") or []) + (payload.get("selected") or [])
        if not isinstance(candidates, list):
            continue
        for raw in candidates:
            if not isinstance(raw, Mapping):
                continue
            timestamp = _candidate_timestamp(raw, payload)
            if not _row_in_window(timestamp, day=day, minutes=window_minutes):
                continue
            sequence += 1
            row = _candidate_row(raw, payload=payload, path=path, sequence=sequence)
            if row["symbol"]:
                rows.append(row)
    return rows


def _variance(values: Sequence[Any]) -> float | None:
    nums = [float(value) for value in (_safe_float(v) for v in values) if value is not None]
    if len(nums) < 2:
        return None
    return round(float(statistics.pvariance(nums)), 6)


def _avg(values: Sequence[Any]) -> float | None:
    nums = [float(value) for value in (_safe_float(v) for v in values) if value is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 4)


def _return_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, field in (
        ("15m", "return_15m_pct"),
        ("30m", "return_30m_pct"),
        ("60m", "return_60m_pct"),
        ("eod", "return_eod_pct"),
    ):
        values = [row.get(field) for row in rows]
        available = [float(v) for v in (_safe_float(value) for value in values) if v is not None]
        out[label] = {
            "available": len(available),
            "average_return_pct": _avg(values),
            "win_rate": round(len([v for v in available if v > 0.0]) / len(available), 4) if available else None,
        }
    return out


def _distribution(values: Sequence[Any]) -> dict[str, Any]:
    nums = sorted(float(v) for v in (_safe_float(value) for value in values) if v is not None)
    if not nums:
        return {"count": 0, "min": None, "p25": None, "median": None, "p75": None, "max": None, "average": None}

    def pct(p: float) -> float:
        if len(nums) == 1:
            return round(nums[0], 4)
        idx = (len(nums) - 1) * p
        lo = int(idx)
        hi = min(lo + 1, len(nums) - 1)
        frac = idx - lo
        return round(nums[lo] + (nums[hi] - nums[lo]) * frac, 4)

    return {
        "count": len(nums),
        "min": round(nums[0], 4),
        "p25": pct(0.25),
        "median": pct(0.50),
        "p75": pct(0.75),
        "max": round(nums[-1], 4),
        "average": _avg(nums),
    }


def _comparison(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "unique_symbols": len({str(row.get("symbol") or "") for row in rows if row.get("symbol")}),
        "spread_pct_distribution": _distribution([row.get("bid_ask_spread_pct") for row in rows]),
        "quote_age_seconds_distribution": _distribution([row.get("quote_age_seconds") for row in rows]),
        "forward_returns": _return_stats([row for row in rows if row.get("forward_returns_available")]),
    }


def _enrich_variance(rows: list[dict[str, Any]]) -> None:
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_symbol[str(row.get("symbol") or "")].append(row)
    for symbol_rows in by_symbol.values():
        spread_variance = _variance([row.get("bid_ask_spread_pct") for row in symbol_rows])
        price_variance = _variance([row.get("price") for row in symbol_rows])
        for row in symbol_rows:
            row["quote_variance"] = {
                "spread_pct_variance": spread_variance,
                "price_variance": price_variance,
                "observations": len(symbol_rows),
            }


def _hypothetical_trade_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "symbol",
        "timestamp",
        "price",
        "bid",
        "ask",
        "bid_ask_spread_dollars",
        "bid_ask_spread_pct",
        "quote_age_seconds",
        "quote_source",
        "quote_variance",
        "gain_pct",
        "rel_volume",
        "return_15m_pct",
        "return_30m_pct",
        "return_60m_pct",
        "return_eod_pct",
        "forward_returns_available",
    )
    return {key: row.get(key) for key in keys}


def _example_rows(rows: Sequence[Mapping[str, Any]], *, limit: int = 50) -> list[dict[str, Any]]:
    return [
        {
            "symbol": row.get("symbol"),
            "timestamp": row.get("timestamp"),
            "price": row.get("price"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "bid_ask_spread_dollars": row.get("bid_ask_spread_dollars"),
            "bid_ask_spread_pct": row.get("bid_ask_spread_pct"),
            "quote_age_seconds": row.get("quote_age_seconds"),
            "quote_source": row.get("quote_source"),
            "quote_variance": row.get("quote_variance"),
            "reject_reason": row.get("reject_reason"),
            "raw_reject_reason": row.get("raw_reject_reason"),
            "return_15m_pct": row.get("return_15m_pct"),
            "return_30m_pct": row.get("return_30m_pct"),
            "return_60m_pct": row.get("return_60m_pct"),
            "return_eod_pct": row.get("return_eod_pct"),
            "forward_returns_available": row.get("forward_returns_available"),
        }
        for row in rows[:limit]
    ]


def _strictness_assessment(*, unstable_rows: Sequence[Mapping[str, Any]], forward_rows: Sequence[Mapping[str, Any]]) -> str:
    if not unstable_rows:
        return "No unstable_quote rejections were found in the first-window sample."
    spreads = [float(v) for v in (_safe_float(row.get("bid_ask_spread_pct")) for row in unstable_rows) if v is not None]
    if not forward_rows:
        if spreads and _avg(spreads) is not None and float(_avg(spreads) or 0.0) > 8.0:
            return (
                "Inconclusive on profitability because local forward returns are unavailable; observed unstable_quote "
                "spreads are mostly above execution caps, so the persisted evidence does not show the threshold is too strict."
            )
        return "Inconclusive on strictness because local forward returns are unavailable."
    avg_30m = _avg([row.get("return_30m_pct") for row in forward_rows])
    win_values = [float(row.get("return_30m_pct")) for row in forward_rows if _safe_float(row.get("return_30m_pct")) is not None]
    win_rate = len([v for v in win_values if v > 0.0]) / len(win_values) if win_values else 0.0
    if avg_30m is not None and avg_30m > 1.0 and win_rate >= 0.6:
        return "Potentially strict: unstable_quote rows with available forward returns had positive 30m outcomes. Review more sessions before changing gates."
    return "Not enough evidence that thresholds are too strict from rows with available forward returns."


def build_unstable_quote_research_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
    window_minutes: int = 30,
) -> dict[str, Any]:
    """Build a first-window unstable quote rejection report without changing trading behavior."""
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
    rows = _load_scan_rows(paths, day=resolved_day, window_minutes=window_minutes)
    _enrich_variance(rows)
    bar_cache: dict[str, list[dict[str, Any]]] = {}
    enriched: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item.update(_forward_returns(item, data_dir=data_path, bars_dir=bars_dir, day=resolved_day, cache=bar_cache))
        enriched.append(item)
    rejected = [row for row in enriched if not row.get("accepted")]
    unstable = [row for row in rejected if row.get("reject_reason") == "unstable_quote"]
    stable = [row for row in enriched if row.get("reject_reason") != "unstable_quote"]
    forward_rows = [row for row in unstable if row.get("forward_returns_available")]
    reason_counts = Counter(str(row.get("reject_reason") or "unknown") for row in rejected)
    symbols = sorted({str(row.get("symbol") or "") for row in unstable if row.get("symbol")})
    by_symbol: dict[str, Any] = {}
    for symbol in symbols:
        sym_rows = [row for row in unstable if row.get("symbol") == symbol]
        by_symbol[symbol] = {
            "rejections": len(sym_rows),
            "average_spread_pct": _avg([row.get("bid_ask_spread_pct") for row in sym_rows]),
            "max_spread_pct": max([float(v) for v in (_safe_float(row.get("bid_ask_spread_pct")) for row in sym_rows) if v is not None], default=None),
            "average_quote_age_seconds": _avg([row.get("quote_age_seconds") for row in sym_rows]),
            "quote_variance": sym_rows[0].get("quote_variance") if sym_rows else None,
            "quote_sources": sorted({str(row.get("quote_source") or "") for row in sym_rows if row.get("quote_source")}),
            "hypothetical_trades": [_hypothetical_trade_row(row) for row in sym_rows],
        }
    return {
        "report": "unstable_quote_research",
        "research_only": True,
        "date": resolved_day,
        "user_id": user_id,
        "window": {
            "label": f"first_{int(window_minutes)}m",
            "start_et": _first_window_bounds(resolved_day, window_minutes)[0].isoformat(),
            "end_et": _first_window_bounds(resolved_day, window_minutes)[1].isoformat(),
        },
        "source_mode": source_mode,
        "source_files": [str(path) for path in paths],
        "summary": {
            "total_candidates": len(enriched),
            "rejected_candidates": len(rejected),
            "unstable_quote_rejections": len(unstable),
            "unstable_quote_unique_symbols": len(symbols),
            "unstable_quote_rejection_rate": round(len(unstable) / len(enriched), 4) if enriched else None,
            "rejection_reason_counts": dict(sorted(reason_counts.items())),
            "average_unstable_spread_pct": _avg([row.get("bid_ask_spread_pct") for row in unstable]),
            "max_unstable_spread_pct": max([float(v) for v in (_safe_float(row.get("bid_ask_spread_pct")) for row in unstable) if v is not None], default=None),
            "forward_return_rows": len(forward_rows),
            "quote_age_rows": len([row for row in unstable if row.get("quote_age_seconds") is not None]),
            "bid_ask_rows": len([row for row in unstable if row.get("bid") is not None and row.get("ask") is not None]),
            "strictness_assessment": _strictness_assessment(unstable_rows=unstable, forward_rows=forward_rows),
        },
        "distributions": {
            "unstable_spread_pct": _distribution([row.get("bid_ask_spread_pct") for row in unstable]),
            "unstable_quote_age_seconds": _distribution([row.get("quote_age_seconds") for row in unstable]),
            "stable_spread_pct": _distribution([row.get("bid_ask_spread_pct") for row in stable]),
            "stable_quote_age_seconds": _distribution([row.get("quote_age_seconds") for row in stable]),
        },
        "stable_vs_unstable": {
            "stable": _comparison(stable),
            "unstable": _comparison(unstable),
        },
        "threshold_context": {
            "dynamic_max_spread_pct": 2.5,
            "strong_catalyst_max_spread_pct": 5.0,
            "execution_max_spread_pct": 8.0,
            "note": "Threshold values are read from current default config for context only; this report does not modify trading behavior.",
        },
        "forward_return_stats": _return_stats(forward_rows),
        "rejected_symbols": symbols,
        "by_symbol": by_symbol,
        "examples": _example_rows(sorted(unstable, key=lambda row: (-(row.get("bid_ask_spread_pct") or 0), row.get("symbol") or "")), limit=100),
    }


def render_unstable_quote_research_report(report: Mapping[str, Any]) -> str:
    """Render the unstable quote report as Markdown."""
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    window = report.get("window") if isinstance(report.get("window"), Mapping) else {}
    lines = [
        f"# Unstable Quote Rejection Report - {report.get('date')}",
        "",
        "Research-only report. No trading behavior changes.",
        "",
        f"- User: `{report.get('user_id')}`",
        f"- Window: `{window.get('start_et')}` to `{window.get('end_et')}`",
        f"- Total first-window candidates: {summary.get('total_candidates')}",
        f"- Rejected candidates: {summary.get('rejected_candidates')}",
        f"- unstable_quote rejections: {summary.get('unstable_quote_rejections')}",
        f"- unstable_quote rejection rate: {summary.get('unstable_quote_rejection_rate')}",
        f"- Average unstable spread: {summary.get('average_unstable_spread_pct')}%",
        f"- Max unstable spread: {summary.get('max_unstable_spread_pct')}%",
        f"- Rows with persisted bid/ask: {summary.get('bid_ask_rows')}",
        f"- Rows with persisted quote age: {summary.get('quote_age_rows')}",
        f"- Rows with forward returns: {summary.get('forward_return_rows')}",
        f"- Assessment: {summary.get('strictness_assessment')}",
        "",
        "## Rejection Counts",
        "",
    ]
    reason_counts = summary.get("rejection_reason_counts") if isinstance(summary.get("rejection_reason_counts"), Mapping) else {}
    if reason_counts:
        for reason, count in reason_counts.items():
            lines.append(f"- {reason}: {count}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Stable vs Unstable",
            "",
            "| Group | Rows | Symbols | Spread Median | Spread P75 | Quote Age Median | Forward Rows | 30m Avg Return |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    comparison = report.get("stable_vs_unstable") if isinstance(report.get("stable_vs_unstable"), Mapping) else {}
    for label in ("stable", "unstable"):
        row = comparison.get(label) if isinstance(comparison.get(label), Mapping) else {}
        spread = row.get("spread_pct_distribution") if isinstance(row.get("spread_pct_distribution"), Mapping) else {}
        age = row.get("quote_age_seconds_distribution") if isinstance(row.get("quote_age_seconds_distribution"), Mapping) else {}
        returns = row.get("forward_returns") if isinstance(row.get("forward_returns"), Mapping) else {}
        ret_30 = returns.get("30m") if isinstance(returns.get("30m"), Mapping) else {}
        lines.append(
            "| {label} | {count} | {symbols} | {spread_med} | {spread_p75} | {age_med} | {fwd} | {ret30} |".format(
                label=label,
                count=row.get("count"),
                symbols=row.get("unique_symbols"),
                spread_med=spread.get("median"),
                spread_p75=spread.get("p75"),
                age_med=age.get("median"),
                fwd=ret_30.get("available"),
                ret30=ret_30.get("average_return_pct"),
            )
        )
    lines.extend(
        [
            "",
            "## Quote Distributions",
            "",
            "| Metric | Count | Min | P25 | Median | P75 | Max | Avg |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    distributions = report.get("distributions") if isinstance(report.get("distributions"), Mapping) else {}
    for label in (
        "unstable_spread_pct",
        "stable_spread_pct",
        "unstable_quote_age_seconds",
        "stable_quote_age_seconds",
    ):
        dist = distributions.get(label) if isinstance(distributions.get(label), Mapping) else {}
        lines.append(
            "| {label} | {count} | {min} | {p25} | {median} | {p75} | {max} | {avg} |".format(
                label=label,
                count=dist.get("count"),
                min=dist.get("min"),
                p25=dist.get("p25"),
                median=dist.get("median"),
                p75=dist.get("p75"),
                max=dist.get("max"),
                avg=dist.get("average"),
            )
        )
    lines.extend(
        [
            "",
            "## Rejected Symbols",
            "",
            "| Symbol | Rejects | Avg Spread | Max Spread | Quote Age | Sources | Spread Variance | Price Variance |",
            "|---|---:|---:|---:|---:|---|---:|---:|",
        ]
    )
    by_symbol = report.get("by_symbol") if isinstance(report.get("by_symbol"), Mapping) else {}
    if by_symbol:
        for symbol, row in by_symbol.items():
            if not isinstance(row, Mapping):
                continue
            variance = row.get("quote_variance") if isinstance(row.get("quote_variance"), Mapping) else {}
            lines.append(
                "| {symbol} | {rejects} | {avg} | {mx} | {age} | {sources} | {spread_var} | {price_var} |".format(
                    symbol=symbol,
                    rejects=row.get("rejections"),
                    avg=row.get("average_spread_pct"),
                    mx=row.get("max_spread_pct"),
                    age=row.get("average_quote_age_seconds"),
                    sources=",".join(row.get("quote_sources") or []) or "n/a",
                    spread_var=variance.get("spread_pct_variance"),
                    price_var=variance.get("price_variance"),
                )
            )
    else:
        lines.append("| n/a | 0 | n/a | n/a | n/a | n/a | n/a | n/a |")
    lines.extend(
        [
            "",
            "## Hypothetical Trades",
            "",
            "| Symbol | Time | Source | Entry | Spread | Quote Age | 15m | 30m | 60m | EOD |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    examples = report.get("examples") if isinstance(report.get("examples"), list) else []
    if examples:
        for row in examples[:100]:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                "| {symbol} | {timestamp} | {source} | {entry} | {spread} | {age} | {r15} | {r30} | {r60} | {eod} |".format(
                    symbol=row.get("symbol"),
                    timestamp=row.get("timestamp"),
                    source=row.get("quote_source") or "n/a",
                    entry=row.get("price"),
                    spread=row.get("bid_ask_spread_pct"),
                    age=row.get("quote_age_seconds"),
                    r15=row.get("return_15m_pct"),
                    r30=row.get("return_30m_pct"),
                    r60=row.get("return_60m_pct"),
                    eod=row.get("return_eod_pct"),
                )
            )
    else:
        lines.append("| n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
    return "\n".join(lines).rstrip() + "\n"


def write_unstable_quote_research_report(
    *,
    data_dir: Path | str = "data",
    day: str,
    user_id: str = "live_bot",
    history_dir: Path | str | None = None,
    bars_dir: Path | str | None = None,
    window_minutes: int = 30,
) -> tuple[Path, Path, dict[str, Any]]:
    """Write JSON and Markdown artifacts for unstable quote research."""
    data_path = Path(data_dir)
    report = build_unstable_quote_research_report(
        data_dir=data_path,
        day=day,
        user_id=user_id,
        history_dir=history_dir,
        bars_dir=bars_dir,
        window_minutes=window_minutes,
    )
    out_dir = data_path / "research" / "unstable_quote_research"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_user = _safe_user(user_id)
    json_path = out_dir / f"{report['date']}_{safe_user}.json"
    text_path = out_dir / f"{report['date']}_{safe_user}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text_path.write_text(render_unstable_quote_research_report(report), encoding="utf-8")
    return json_path, text_path, report
