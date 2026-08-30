"""Read-only strategy quality research report."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from src.trade_attribution import attribution_daily_path, load_daily_artifact


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return out if math.isfinite(out) else default


def _rows(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    rows = payload.get(key)
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _route(row: Mapping[str, Any]) -> str:
    return str(row.get("sleeve") or row.get("entry_route") or row.get("route") or "unknown").strip() or "unknown"


def _profit_factor(pnls: list[float]) -> float | None:
    gains = sum(v for v in pnls if v > 0.0)
    losses = abs(sum(v for v in pnls if v < 0.0))
    if losses <= 0.0:
        return None if gains <= 0.0 else float("inf")
    return gains / losses


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _feature_buckets(rows: list[Mapping[str, Any]], *, winners: bool) -> Counter[str]:
    out: Counter[str] = Counter()
    for row in rows:
        pnl = _safe_float(row.get("pnl", row.get("realized_pnl")))
        if winners and pnl <= 0.0:
            continue
        if not winners and pnl >= 0.0:
            continue
        for key in (
            "symbol_above_vwap",
            "spy_above_vwap",
            "qqq_above_vwap",
            "sector_above_vwap",
            "alignment_5m",
            "trend_15m",
            "premarket_injected",
        ):
            if key in row:
                out[f"{key}={str(bool(row.get(key))).lower()}"] += 1
        score = row.get("trend_long_quality_score")
        if score is not None:
            out[f"quality_score_bucket={int(_safe_float(score) // 2 * 2)}"] += 1
    return out


def build_strategy_quality_report(
    *,
    data_dir: Path | str,
    reports_dir: Path | str,
    user_id: str,
    day: str,
) -> dict[str, Any]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    payload = load_daily_artifact(path) if path.exists() else {}
    exits = _rows(payload, "exits")
    candidates = _rows(payload, "candidates")
    rejected = _rows(payload, "rejected_one_rule")
    by_sleeve: dict[str, dict[str, Any]] = {}
    grouped: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in exits:
        grouped[_route(row)].append(row)
    for sleeve, rows in sorted(grouped.items()):
        pnls = [_safe_float(row.get("pnl", row.get("realized_pnl"))) for row in rows]
        wins = sum(1 for pnl in pnls if pnl > 0.0)
        by_sleeve[sleeve] = {
            "trades": len(rows),
            "pnl": round(sum(pnls), 6),
            "win_rate": wins / len(rows) if rows else 0.0,
            "profit_factor": _profit_factor(pnls),
            "avg_mfe_pct": _avg([_safe_float(row.get("max_favorable_excursion_pct", row.get("mfe_pct"))) for row in rows if row.get("max_favorable_excursion_pct", row.get("mfe_pct")) is not None]),
            "avg_mae_pct": _avg([_safe_float(row.get("max_adverse_excursion_pct", row.get("mae_pct"))) for row in rows if row.get("max_adverse_excursion_pct", row.get("mae_pct")) is not None]),
            "avg_holding_minutes": _avg([_safe_float(row.get("holding_minutes", row.get("hold_minutes"))) for row in rows if row.get("holding_minutes", row.get("hold_minutes")) is not None]),
        }
    quality_scores = [
        _safe_float(row.get("trend_long_quality_score"))
        for row in candidates + exits
        if row.get("trend_long_quality_score") is not None
    ]
    entry_scores = [
        _safe_float(row.get("entry_quality_score"))
        for row in candidates + exits
        if row.get("entry_quality_score") is not None
    ]
    score_hist = Counter(str(int(score // 10 * 10)) for score in entry_scores)
    adaptive_rows = [
        row for row in exits
        if bool(row.get("adaptive_entry") or row.get("entry_quality_adaptive_market_vwap"))
    ]
    blocked_adaptive = [
        row for row in candidates + rejected
        if row.get("entry_quality_score") is not None and not bool(row.get("adaptive_entry"))
    ]
    market_vwap_only = [
        row for row in candidates + exits + rejected
        if str(row.get("entry_quality_reason") or row.get("rejected_rule") or "").endswith("market_vwap_not_confirmed")
        or bool(row.get("entry_quality_adaptive_market_vwap"))
    ]
    adaptive_pnls = [_safe_float(row.get("pnl", row.get("realized_pnl"))) for row in adaptive_rows]
    removed_reasons = Counter()
    for row in candidates + exits:
        penalties = row.get("entry_quality_penalties")
        if isinstance(penalties, list):
            for penalty in penalties:
                removed_reasons[str(penalty)] += 1
    rejected_counts = Counter(str(row.get("rejected_rule") or "unknown") for row in rejected)
    dynamic_no_catalyst = [
        row for row in exits if "dynamic" in _route(row).lower() and _safe_float(row.get("catalyst_score")) <= 0.0
    ]
    recommendations: list[str] = []
    trend = by_sleeve.get("trend_long")
    if trend and trend["trades"] and trend["win_rate"] <= 0.25:
        recommendations.append("Raise live_min_quality_score or keep trend_long disabled in weak regimes.")
    if rejected_counts:
        top_rule, count = rejected_counts.most_common(1)[0]
        recommendations.append(f"Review {top_rule}: {count} one-rule rejected candidates were recorded.")
    if not recommendations:
        recommendations.append("No config change recommended from available data.")
    report = {
        "date": day,
        "user_id": user_id,
        "source": str(path),
        "pnl_by_sleeve": {sleeve: row["pnl"] for sleeve, row in by_sleeve.items()},
        "sleeves": by_sleeve,
        "top_losing_features": dict(_feature_buckets(exits, winners=False).most_common(10)),
        "top_winning_features": dict(_feature_buckets(exits, winners=True).most_common(10)),
        "rejected_one_rule": {
            "total": len(rejected),
            "by_rule": dict(rejected_counts),
        },
        "trend_long_quality_score_distribution": {
            "count": len(quality_scores),
            "avg": _avg(quality_scores),
            "min": min(quality_scores) if quality_scores else None,
            "max": max(quality_scores) if quality_scores else None,
        },
        "entry_quality_adaptive_scoring": {
            "average_entry_score": _avg(entry_scores),
            "median_entry_score": _median(entry_scores),
            "score_histogram": dict(score_hist),
            "thresholds_used": dict(Counter(str(row.get("entry_quality_threshold")) for row in candidates + exits if row.get("entry_quality_threshold") is not None)),
            "reasons_removed_by_adaptive_scoring": dict(removed_reasons),
            "adaptive_entries": len(adaptive_rows),
            "blocked_despite_adaptive_scoring": len(blocked_adaptive),
            "market_vwap_only_failures": len(market_vwap_only),
            "adaptive_entries_pnl": round(sum(adaptive_pnls), 6),
        },
        "dynamic_no_catalyst_results": {
            "trades": len(dynamic_no_catalyst),
            "pnl": round(sum(_safe_float(row.get("pnl", row.get("realized_pnl"))) for row in dynamic_no_catalyst), 6),
        },
        "recommended_config_changes": recommendations,
    }
    adaptive_path = Path(data_dir) / "research_metrics" / day / "dynamic_entry_adaptive.json"
    if adaptive_path.exists():
        try:
            adaptive = json.loads(adaptive_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            adaptive = {}
        if isinstance(adaptive, Mapping):
            report["dynamic_entry_adaptive"] = {
                "current_mode": adaptive.get("current_mode"),
                "reason_for_mode": adaptive.get("reason_for_mode"),
                "effective_thresholds": adaptive.get("effective_thresholds"),
                "actual_trade_frequency": adaptive.get("actual_trade_frequency"),
                "top_rejection_reasons": adaptive.get("top_rejection_reasons"),
                "safety_trigger_status": adaptive.get("safety_trigger_status"),
            }
    out = Path(reports_dir) / "strategy_quality" / f"{day}_dashboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["dashboard_path"] = str(out)
    return report


def render_strategy_quality_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"Strategy Quality Report {report.get('date')} [{report.get('user_id')}]",
        f"Dashboard: {report.get('dashboard_path')}",
        "PnL by sleeve: "
        + (
            ", ".join(f"{k}=${_safe_float(v):.2f}" for k, v in sorted((report.get("pnl_by_sleeve") or {}).items()))
            or "none"
        ),
    ]
    sleeves = report.get("sleeves") if isinstance(report.get("sleeves"), Mapping) else {}
    for sleeve, row in sorted(sleeves.items()):
        if not isinstance(row, Mapping):
            continue
        pf = row.get("profit_factor")
        pf_s = "inf" if pf == float("inf") else ("n/a" if pf is None else f"{_safe_float(pf):.2f}")
        lines.append(
            f"{sleeve}: trades={int(_safe_float(row.get('trades')))} "
            f"win_rate={_safe_float(row.get('win_rate')) * 100.0:.1f}% "
            f"pf={pf_s} avg_mfe={_safe_float(row.get('avg_mfe_pct')):.2f}% "
            f"avg_mae={_safe_float(row.get('avg_mae_pct')):.2f}% "
            f"avg_hold={_safe_float(row.get('avg_holding_minutes')):.1f}m"
        )
    rejected = report.get("rejected_one_rule") if isinstance(report.get("rejected_one_rule"), Mapping) else {}
    lines.append(f"Rejected one-rule candidates: total={int(_safe_float(rejected.get('total')))} by_rule={rejected.get('by_rule') or {}}")
    dist = report.get("trend_long_quality_score_distribution") if isinstance(report.get("trend_long_quality_score_distribution"), Mapping) else {}
    lines.append(
        "Trend Long quality scores: "
        f"count={int(_safe_float(dist.get('count')))} avg={_safe_float(dist.get('avg')):.2f} "
        f"min={dist.get('min')} max={dist.get('max')}"
    )
    dyn = report.get("dynamic_no_catalyst_results") if isinstance(report.get("dynamic_no_catalyst_results"), Mapping) else {}
    lines.append(f"Dynamic no-catalyst: trades={int(_safe_float(dyn.get('trades')))} pnl=${_safe_float(dyn.get('pnl')):.2f}")
    adaptive_quality = report.get("entry_quality_adaptive_scoring") if isinstance(report.get("entry_quality_adaptive_scoring"), Mapping) else {}
    if adaptive_quality:
        lines.append(
            "Entry quality adaptive scoring: "
            f"average_entry_score={adaptive_quality.get('average_entry_score')} "
            f"median_entry_score={adaptive_quality.get('median_entry_score')} "
            f"score_histogram={adaptive_quality.get('score_histogram')} "
            f"thresholds_used={adaptive_quality.get('thresholds_used')} "
            f"reasons_removed={adaptive_quality.get('reasons_removed_by_adaptive_scoring')} "
            f"adaptive_entries={adaptive_quality.get('adaptive_entries')} "
            f"blocked_despite_adaptive={adaptive_quality.get('blocked_despite_adaptive_scoring')} "
            f"market_vwap_only_failures={adaptive_quality.get('market_vwap_only_failures')} "
            f"adaptive_entries_pnl=${_safe_float(adaptive_quality.get('adaptive_entries_pnl')):.2f}"
        )
    adaptive = report.get("dynamic_entry_adaptive") if isinstance(report.get("dynamic_entry_adaptive"), Mapping) else {}
    if adaptive:
        lines.append(
            "Dynamic adaptive: "
            f"mode={adaptive.get('current_mode')} reason={adaptive.get('reason_for_mode')} "
            f"thresholds={adaptive.get('effective_thresholds')} "
            f"actual_trade_frequency={adaptive.get('actual_trade_frequency')} "
            f"safety={adaptive.get('safety_trigger_status')}"
        )
    lines.append("Top losing features: " + (", ".join(f"{k}:{v}" for k, v in (report.get("top_losing_features") or {}).items()) or "none"))
    lines.append("Top winning features: " + (", ".join(f"{k}:{v}" for k, v in (report.get("top_winning_features") or {}).items()) or "none"))
    lines.append("Recommended config changes: " + " | ".join(report.get("recommended_config_changes") or []))
    return "\n".join(lines)
