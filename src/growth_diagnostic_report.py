"""Read-only live growth diagnostics from recent session artifacts."""
from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping


ROUTES = (
    "core_rebuild",
    "dynamic_momentum",
    "dynamic_momentum_override",
    "news_catalyst",
    "trend_long",
    "momentum_breakout",
    "high_gainer_exceptional",
    "high_gainer_standard",
    "options_paper",
    "options_live",
)


@dataclass(frozen=True)
class GrowthDiagnosticArtifacts:
    json_path: Path
    html_path: Path
    report: dict[str, Any]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def available_live_dates(root: Path, user_id: str) -> list[str]:
    dates: set[str] = set()
    for path in (root / "data" / "research_metrics").glob("*/end_day_live.json"):
        dates.add(path.parent.name)
    for path in (root / "data" / "profitability_attribution" / "daily").glob(f"*_ {user_id}.json"):
        dates.add(path.name.split("_", 1)[0])
    for path in (root / "data" / "profitability_attribution" / "daily").glob(f"*_{user_id}.json"):
        dates.add(path.name.split("_", 1)[0])
    return sorted(dates)


def _latest_dates(root: Path, user_id: str, *, end_date: str, lookback_days: int) -> list[str]:
    dates = available_live_dates(root, user_id)
    if end_date != "latest":
        dates = [day for day in dates if day <= end_date]
    return dates[-max(1, lookback_days):]


def _parse_summary_pnl(stdout: str) -> dict[str, float]:
    match = re.search(r"PnL:\s+realized=\$([-0-9.]+)\s+unrealized=\$([-0-9.]+)\s+total=\$([-0-9.]+)", stdout or "")
    if not match:
        return {"realized": 0.0, "unrealized": 0.0, "total": 0.0}
    return {"realized": float(match.group(1)), "unrealized": float(match.group(2)), "total": float(match.group(3))}


def _parse_positions(stdout: str) -> dict[str, Any]:
    equity_match = re.search(r"Equity:\s+\$([0-9,.-]+)", stdout or "")
    total_match = re.search(r"TOTAL\s+\$[\s]*([0-9,.-]+)\s+\$[\s]*([+-]?[0-9,.-]+)", stdout or "")
    equity = _as_float((equity_match.group(1) if equity_match else "0").replace(",", ""))
    exposure = _as_float((total_match.group(1) if total_match else "0").replace(",", ""))
    unreal = _as_float((total_match.group(2) if total_match else "0").replace(",", ""))
    return {
        "equity": equity,
        "open_market_value": exposure,
        "open_unrealized": unreal,
        "cash_utilization_pct": round((exposure / equity * 100.0), 4) if equity > 0 else 0.0,
    }


def _session_report(root: Path, day: str, user_id: str) -> dict[str, Any]:
    end_day = _load_json(root / "data" / "research_metrics" / day / "end_day_live.json")
    attrib = _load_json(root / "data" / "profitability_attribution" / "daily" / f"{day}_{user_id}.json")
    context = end_day.get("context") if isinstance(end_day.get("context"), Mapping) else {}
    summary_stdout = ((context.get("account_summary") or {}).get("stdout") if isinstance(context.get("account_summary"), Mapping) else "") or ""
    positions_stdout = ((context.get("positions") or {}).get("stdout") if isinstance(context.get("positions"), Mapping) else "") or ""
    logs = end_day.get("logs") if isinstance(end_day.get("logs"), Mapping) else {}
    dynamic = logs.get("dynamic") if isinstance(logs.get("dynamic"), Mapping) else {}
    entry = logs.get("entry_lane") if isinstance(logs.get("entry_lane"), Mapping) else {}
    allocator = logs.get("allocator") if isinstance(logs.get("allocator"), Mapping) else {}
    orders = logs.get("orders") if isinstance(logs.get("orders"), Mapping) else {}
    dynamic_funnel = _load_json(root / "data" / "research_metrics" / day / "dynamic_funnel_live.json")
    funnel_recon = dynamic_funnel.get("reconciliation") if isinstance(dynamic_funnel.get("reconciliation"), Mapping) else {}
    order_recon = dynamic_funnel.get("order_attribution_reconciliation") if isinstance(dynamic_funnel.get("order_attribution_reconciliation"), Mapping) else {}
    pnl = attrib.get("overall_pnl") if isinstance(attrib.get("overall_pnl"), Mapping) else _parse_summary_pnl(summary_stdout)
    route_stats = attrib.get("route_stats") if isinstance(attrib.get("route_stats"), Mapping) else {}
    route_pnl = attrib.get("pnl_by_route") if isinstance(attrib.get("pnl_by_route"), Mapping) else {}
    submitted_fallback = _as_int(funnel_recon.get("submitted_orders")) or _as_int(orders.get("submitted_count", orders.get("confirmation_count")))
    filled_fallback = _as_int(funnel_recon.get("fills")) or _as_int(orders.get("filled_count"))
    exits_fallback = sum(_as_int((row or {}).get("exits")) for row in (attrib.get("exit_reason_stats") or {}).values()) if isinstance(attrib.get("exit_reason_stats"), Mapping) else 0
    if not order_recon or not any(_as_int(value) for value in order_recon.values()):
        order_recon = {
            "submitted": submitted_fallback,
            "accepted": 0,
            "partially_filled": 0,
            "filled": filled_fallback,
            "cancelled": 0,
            "rejected": 0,
            "attributed": exits_fallback,
            "missing_attribution": max(0, submitted_fallback - exits_fallback),
            "duplicate_records": 0,
        }
    return {
        "date": day,
        "pnl": {
            "realized": _as_float(pnl.get("realized")),
            "unrealized": _as_float(pnl.get("unrealized")),
            "total": _as_float(pnl.get("total")),
        },
        "positions": _parse_positions(positions_stdout),
        "opportunity_funnel": {
            "scanner_candidates": _as_int(funnel_recon.get("scanner_events")) or _as_int(dynamic.get("selected_count")) + sum(_as_int(v) for v in (dynamic.get("rejected_reasons") or {}).values()),
            "high_gainer_candidates": _as_int(dynamic.get("high_gainer_candidates")),
            "dynamic_candidates": _as_int(dynamic.get("selected_count")),
            "trend_long_candidates": _as_int(((entry.get("by_route") or {}).get("trend_long") or {}).get("total")),
            "news_catalyst_candidates": _as_int(((entry.get("by_route") or {}).get("news_catalyst") or {}).get("total")),
            "entry_evaluations": _as_int(funnel_recon.get("entry_evaluations")),
            "alignment_rejections": _as_int(funnel_recon.get("alignment_rejections")),
            "entry_eval_passed": len(entry.get("pass_symbols") or []),
            "allocator_candidates": len(entry.get("allocator_trace_symbols") or []),
            "allocator_actions": _as_int(allocator.get("actions_count")),
            "submitted_orders": submitted_fallback,
            "filled_orders": filled_fallback,
            "exits": exits_fallback,
            "missing_attribution": _as_int(re.search(r"pnl_missing_exits=(\d+)", summary_stdout or "").group(1)) if re.search(r"pnl_missing_exits=(\d+)", summary_stdout or "") else 0,
        },
        "route_stats": route_stats,
        "route_pnl": route_pnl,
        "top_losers": attrib.get("top_losers") or [],
        "top_winners": attrib.get("top_winners") or [],
        "pipeline_drop_reasons": dict(allocator.get("reject_reasons") or {}),
        "order_attribution_reconciliation": dict(order_recon),
        "risk_guards": re.search(r"Risk guards:\s*(.*)", summary_stdout or "").group(1) if re.search(r"Risk guards:\s*(.*)", summary_stdout or "") else "",
        "source_files": {
            "end_day": str(root / "data" / "research_metrics" / day / "end_day_live.json"),
            "attribution": str(root / "data" / "profitability_attribution" / "daily" / f"{day}_{user_id}.json"),
        },
    }


def _route_expectancy(sessions: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    aggregate: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for session in sessions:
        for route, stats in (session.get("route_stats") or {}).items():
            if not isinstance(stats, Mapping):
                continue
            row = aggregate[str(route)]
            row["trades"] += _as_float(stats.get("trades"))
            row["wins"] += _as_float(stats.get("wins"))
            row["losses"] += _as_float(stats.get("losses"))
            row["pnl"] += _as_float((session.get("route_pnl") or {}).get(route))
    output: dict[str, dict[str, Any]] = {}
    for route in sorted(set(ROUTES).union(aggregate)):
        row = aggregate[route]
        trades = row.get("trades", 0.0)
        pnl = row.get("pnl", 0.0)
        output[route] = {
            "trades": int(trades),
            "win_rate": round(row.get("wins", 0.0) / trades, 4) if trades else None,
            "contribution_pnl": round(pnl, 4),
            "expectancy": round(pnl / trades, 4) if trades else None,
            "recommendation": "do_not_increase_size" if trades == 0 or pnl <= 0 else "eligible_for_review",
        }
    return output


def _stale_positions(root: Path) -> list[dict[str, Any]]:
    payload = _load_json(root / "data" / "positions_live_bot.json")
    positions = payload.get("positions") if isinstance(payload.get("positions"), Mapping) else payload
    rows: list[dict[str, Any]] = []
    if not isinstance(positions, Mapping):
        return rows
    today = date.today()
    for symbol, pos in positions.items():
        if not isinstance(pos, Mapping):
            continue
        opened = str(pos.get("entry_time") or pos.get("opened_at") or pos.get("timestamp") or "")
        age_days = None
        if len(opened) >= 10:
            try:
                age_days = (today - date.fromisoformat(opened[:10])).days
            except ValueError:
                age_days = None
        pnl_pct = _as_float(pos.get("unrealized_plpc", pos.get("pnl_pct")), 0.0)
        if (age_days is not None and age_days >= 5) or pnl_pct < -0.02:
            rows.append({"symbol": str(symbol), "age_days": age_days, "pnl_pct": pnl_pct, "reason": "older_than_strategy_window_or_below_stop_review"})
    return rows


def build_growth_diagnostic_report(root: Path, *, end_date: str = "latest", lookback_days: int = 10, user_id: str = "live_bot") -> dict[str, Any]:
    days = _latest_dates(root, user_id, end_date=end_date, lookback_days=lookback_days)
    sessions = [_session_report(root, day, user_id) for day in days]
    funnel_totals: Counter[str] = Counter()
    pnl_total = 0.0
    realized = 0.0
    unrealized = 0.0
    cash_utilization = []
    top_losers: list[dict[str, Any]] = []
    drop_reasons: Counter[str] = Counter()
    missing_attribution = 0
    for session in sessions:
        for key, value in (session.get("opportunity_funnel") or {}).items():
            funnel_totals[key] += _as_int(value)
        pnl_total += _as_float((session.get("pnl") or {}).get("total"))
        realized += _as_float((session.get("pnl") or {}).get("realized"))
        unrealized += _as_float((session.get("pnl") or {}).get("unrealized"))
        cash_utilization.append(_as_float((session.get("positions") or {}).get("cash_utilization_pct")))
        top_losers.extend(dict(row, date=session["date"]) for row in session.get("top_losers") or [])
        missing_attribution += _as_int((session.get("opportunity_funnel") or {}).get("missing_attribution"))
        for reason, count in (session.get("pipeline_drop_reasons") or {}).items():
            drop_reasons[str(reason)[:160]] += _as_int(count)
    expectancy = _route_expectancy(sessions)
    defects = []
    if missing_attribution:
        defects.append("reporting_defect")
    if any((row.get("expectancy") or 0) < 0 for row in expectancy.values() if row.get("expectancy") is not None):
        defects.append("strategy_defect")
    if funnel_totals["allocator_actions"] > funnel_totals["submitted_orders"]:
        defects.append("execution_defect")
    if not sessions:
        defects.append("insufficient_sample_size")
    confidence = "medium" if len(sessions) >= min(10, lookback_days) else "low"
    recommendation = "Do not increase sizing for negative/unknown expectancy routes; focus on terminal pipeline accountability, attribution repair, and guarded starter sizing only for routes with observed positive expectancy."
    return {
        "user_id": user_id,
        "lookback_days": lookback_days,
        "dates": days,
        "account_equity_change": {"total_pnl": round(pnl_total, 4), "realized": round(realized, 4), "unrealized": round(unrealized, 4)},
        "cash_utilization": {"average_pct": round(sum(cash_utilization) / len(cash_utilization), 4) if cash_utilization else 0.0},
        "opportunity_funnel": dict(funnel_totals),
        "route_expectancy": expectancy,
        "top_entered_losers": sorted(top_losers, key=lambda row: _as_float(row.get("pnl")))[:10],
        "top_mfe_givebacks": [],
        "top_missed_winners": [],
        "pipeline_drop_reasons": drop_reasons.most_common(10),
        "sizing_blocks": [row for row, _count in drop_reasons.items() if "min_realloc" in row or "min_trade" in row or "size" in row],
        "stale_positions": _stale_positions(root),
        "order_attribution_reconciliation": {
            "submitted": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("submitted")) for session in sessions),
            "accepted": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("accepted")) for session in sessions),
            "partially_filled": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("partially_filled")) for session in sessions),
            "filled": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("filled")) for session in sessions),
            "cancelled": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("cancelled")) for session in sessions),
            "rejected": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("rejected")) for session in sessions),
            "attributed": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("attributed")) for session in sessions),
            "missing_attribution": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("missing_attribution")) for session in sessions),
            "duplicate_records": sum(_as_int((session.get("order_attribution_reconciliation") or {}).get("duplicate_records")) for session in sessions),
        },
        "defect_classes": sorted(set(defects)) or ["insufficient_sample_size"],
        "recommended_action": recommendation,
        "confidence": confidence,
        "config_changes_justified": False,
        "sessions": sessions,
    }


def render_growth_diagnostic_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Growth Diagnostic Report",
        "",
        f"User: {report.get('user_id')}",
        f"Dates: {', '.join(report.get('dates') or []) or 'none'}",
        f"Total P/L: ${((report.get('account_equity_change') or {}).get('total_pnl') or 0):.2f}",
        f"Average cash utilization: {((report.get('cash_utilization') or {}).get('average_pct') or 0):.2f}%",
        f"Confidence: {report.get('confidence')}",
        f"Defect classes: {', '.join(report.get('defect_classes') or [])}",
        "",
        "## Opportunity Funnel",
    ]
    for key, value in (report.get("opportunity_funnel") or {}).items():
        lines.append(f"- {key}: {value}")
    recon = report.get("order_attribution_reconciliation") if isinstance(report.get("order_attribution_reconciliation"), Mapping) else {}
    lines.extend(
        [
            "",
            "## Order Attribution Reconciliation",
            "ORDER_ATTRIBUTION_RECONCILIATION "
            f"submitted={recon.get('submitted', 0)} "
            f"accepted={recon.get('accepted', 0)} "
            f"partially_filled={recon.get('partially_filled', 0)} "
            f"filled={recon.get('filled', 0)} "
            f"cancelled={recon.get('cancelled', 0)} "
            f"rejected={recon.get('rejected', 0)} "
            f"attributed={recon.get('attributed', 0)} "
            f"missing_attribution={recon.get('missing_attribution', 0)} "
            f"duplicate_records={recon.get('duplicate_records', 0)}",
        ]
    )
    lines.extend(["", "## Route Expectancy"])
    for route, row in (report.get("route_expectancy") or {}).items():
        lines.append(f"- {route}: trades={row.get('trades')} pnl=${row.get('contribution_pnl')} expectancy={row.get('expectancy')} recommendation={row.get('recommendation')}")
    lines.extend(["", "## Recommended Action", str(report.get("recommended_action") or "")])
    return "\n".join(lines) + "\n"


def write_growth_diagnostic_report(root: Path, *, end_date: str = "latest", lookback_days: int = 10, user_id: str = "live_bot") -> GrowthDiagnosticArtifacts:
    report = build_growth_diagnostic_report(root, end_date=end_date, lookback_days=lookback_days, user_id=user_id)
    out_dir = root / "reports" / "growth_diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = (report.get("dates") or ["latest"])[-1]
    json_path = out_dir / f"{suffix}_{user_id}_growth_diagnostic.json"
    html_path = out_dir / f"{suffix}_{user_id}_growth_diagnostic.html"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    text = render_growth_diagnostic_report(report)
    html_path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Growth Diagnostic</title></head>"
        f"<body><pre>{html.escape(text)}</pre></body></html>",
        encoding="utf-8",
    )
    return GrowthDiagnosticArtifacts(json_path=json_path, html_path=html_path, report=report)
