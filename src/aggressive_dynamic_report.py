"""Read-only aggressive dynamic-entry comparison report."""

from __future__ import annotations

import json
import math
from collections import Counter
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


def _is_aggressive(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("aggressive_dynamic_mode")
        or row.get("aggressive_fast_lane")
        or row.get("aggressive_dynamic_score") is not None
    )


def _price_tier(row: Mapping[str, Any]) -> str:
    text = str(row.get("price_tier") or "").strip()
    if text:
        return text
    price = _safe_float(row.get("price", row.get("entry_price")), -1.0)
    if price < 0:
        return "unknown"
    if price < 2:
        return "sub_2"
    if price < 5:
        return "two_to_5"
    if price < 20:
        return "five_to_20"
    return "above_20"


def build_aggressive_dynamic_report(*, data_dir: Path | str, user_id: str, day: str) -> dict[str, Any]:
    path = attribution_daily_path(data_dir=data_dir, user_id=user_id, day=day)
    payload = load_daily_artifact(path) if path.exists() else {}
    candidates = [row for row in _rows(payload, "candidates") if _is_aggressive(row)]
    orders = [row for row in _rows(payload, "orders") if _is_aggressive(row)]
    exits = [row for row in _rows(payload, "exits") if _is_aggressive(row)]
    fills = [
        row
        for row in orders
        if str(row.get("status") or row.get("fill_status") or "").strip().lower()
        in {"filled", "partially_filled"}
    ]
    pnls = [_safe_float(row.get("pnl", row.get("realized_pnl"))) for row in exits]
    winners = sum(1 for pnl in pnls if pnl > 0.0)
    losses = abs(sum(pnl for pnl in pnls if pnl < 0.0))
    gains = sum(pnl for pnl in pnls if pnl > 0.0)
    score_buckets = Counter(str(int(_safe_float(row.get("aggressive_dynamic_score")) // 10 * 10)) for row in candidates + exits)
    failure_combos = Counter(",".join(str(v) for v in row.get("bypassed_noncritical_rules") or []) or "none" for row in candidates)
    return {
        "date": day,
        "user_id": user_id,
        "source": str(path),
        "normal_rejected_aggressive_accepted": sum(1 for row in candidates if row.get("normal_decision") in {False, "rejected"}),
        "aggressive_candidates": len(candidates),
        "submitted": len(orders),
        "filled": len(fills),
        "winners": winners,
        "losers": sum(1 for pnl in pnls if pnl < 0.0),
        "win_rate": winners / len(pnls) if pnls else 0.0,
        "expectancy": sum(pnls) / len(pnls) if pnls else None,
        "profit_factor": None if losses <= 0.0 else gains / losses,
        "average_mfe": None if not exits else sum(_safe_float(row.get("mfe_pct", row.get("max_favorable_excursion_pct"))) for row in exits) / len(exits),
        "average_mae": None if not exits else sum(_safe_float(row.get("mae_pct", row.get("max_adverse_excursion_pct"))) for row in exits) / len(exits),
        "net_incremental_pnl": round(sum(pnls), 6),
        "score_buckets": dict(score_buckets),
        "failure_factor_combinations": dict(failure_combos),
        "by_catalyst_type": dict(Counter(str(row.get("catalyst_type") or "none") for row in candidates + exits)),
        "by_price_tier": dict(Counter(_price_tier(row) for row in candidates + exits)),
        "by_regime": dict(Counter(str(row.get("market_regime_label") or row.get("market_regime_score") or "unknown") for row in candidates + exits)),
        "by_time_of_day": dict(Counter(str(row.get("timestamp") or row.get("entry_time") or "")[11:13] or "unknown" for row in candidates + exits)),
    }


def render_aggressive_dynamic_report(report: Mapping[str, Any]) -> str:
    pf = report.get("profit_factor")
    pf_text = "n/a" if pf is None else "%.2f" % _safe_float(pf)
    return "\n".join(
        [
            f"Aggressive Dynamic Report {report.get('date')} [{report.get('user_id')}]",
            f"normal_rejected_aggressive_accepted={report.get('normal_rejected_aggressive_accepted')}",
            f"submitted={report.get('submitted')} filled={report.get('filled')} winners={report.get('winners')} losers={report.get('losers')}",
            f"win_rate={_safe_float(report.get('win_rate')) * 100.0:.1f}% expectancy={report.get('expectancy')} profit_factor={pf_text}",
            f"avg_mfe={report.get('average_mfe')} avg_mae={report.get('average_mae')} net_incremental_pnl={report.get('net_incremental_pnl')}",
            f"score_buckets={report.get('score_buckets')}",
            f"failure_factor_combinations={report.get('failure_factor_combinations')}",
            f"by_catalyst_type={report.get('by_catalyst_type')}",
            f"by_price_tier={report.get('by_price_tier')}",
            f"by_regime={report.get('by_regime')}",
            f"by_time_of_day={report.get('by_time_of_day')}",
        ]
    )


def write_aggressive_dynamic_report(*, data_dir: Path | str, reports_dir: Path | str, user_id: str, day: str) -> tuple[Path, dict[str, Any]]:
    report = build_aggressive_dynamic_report(data_dir=data_dir, user_id=user_id, day=day)
    out = Path(reports_dir) / "aggressive_dynamic" / f"{day}_{user_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["dashboard_path"] = str(out)
    return out, report
