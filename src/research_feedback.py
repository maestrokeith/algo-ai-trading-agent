"""Read-only research feedback reports from daily trading analytics artifacts."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.catalyst_outcomes import load_catalyst_outcome_records
from src.profitability_attribution import (
    build_profitability_report,
    discover_replay_summary_path,
    load_profitability_report_inputs,
    load_trade_churn_analysis,
)
from src.report_dates import latest_report_date


@dataclass(frozen=True)
class ResearchTrade:
    """Normalized row used by the research feedback engine."""

    symbol: str
    return_pct: float
    pnl: float | None = None
    catalyst_type: str = "unknown"
    news_score: float | None = None
    relative_volume: float | None = None
    sector: str = "unknown"
    exit_reason: str = "unknown"
    hold_minutes: float | None = None
    route: str = "unknown"
    source: str = "trade_attribution"


def _safe_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _load_json(path: Path | str | None) -> Any | None:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        return None
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _rows(payload: Any, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, Mapping)]
    return []


def _first(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _return_pct(row: Mapping[str, Any]) -> float | None:
    direct = _safe_float(_first(row, ("return_pct", "pnl_pct", "realized_return_pct", "profit_loss_pct")))
    if direct is not None:
        return direct
    pnl = _safe_float(row.get("pnl"))
    qty = _safe_float(row.get("qty"))
    price = _safe_float(_first(row, ("filled_avg_price", "entry_price", "avg_entry_price")))
    if pnl is None or qty is None or price is None or abs(qty * price) <= 0.0:
        return None
    return (pnl / abs(qty * price)) * 100.0


def _label(value: Any) -> str:
    text = str(value or "unknown").strip().lower().replace(" ", "_")
    return text or "unknown"


def _news_score_bin(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 4.0:
        return "0-3"
    if value < 7.0:
        return "4-6"
    return "7-10"


def _relative_volume_bin(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 1.0:
        return "<1x"
    if value < 2.0:
        return "1-2x"
    if value < 5.0:
        return "2-5x"
    return "5x+"


def _hold_bin(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 15.0:
        return "<15m"
    if value < 30.0:
        return "15-30m"
    if value < 90.0:
        return "30-90m"
    return "90m+"


def _trade_from_exit(row: Mapping[str, Any]) -> ResearchTrade | None:
    ret = _return_pct(row)
    pnl = _safe_float(row.get("pnl"))
    if ret is None and pnl is None:
        return None
    return ResearchTrade(
        symbol=str(row.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN",
        return_pct=float(ret if ret is not None else 0.0),
        pnl=pnl,
        catalyst_type=_label(_first(row, ("catalyst_type", "news_catalyst_type", "event_type"))),
        news_score=_safe_float(_first(row, ("news_score", "entry_news_score"))),
        relative_volume=_safe_float(_first(row, ("relative_volume", "rel_volume", "entry_relative_volume"))),
        sector=_label(_first(row, ("sector", "theme", "bucket", "industry"))),
        exit_reason=_label(_first(row, ("exit_reason", "reason", "sell_reason", "exit_type"))),
        hold_minutes=_safe_float(_first(row, ("hold_minutes", "duration_minutes", "hold_duration_minutes"))),
        route=_label(_first(row, ("entry_route", "route", "strategy", "source"))),
        source="trade_attribution",
    )


def _trade_from_catalyst(row: Mapping[str, Any]) -> ResearchTrade | None:
    ret = _safe_float(_first(row, ("realized_return_pct", "subsequent_return_pct", "return_pct")))
    if ret is None:
        return None
    return ResearchTrade(
        symbol=str(row.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN",
        return_pct=ret,
        pnl=None,
        catalyst_type=_label(row.get("catalyst_type")),
        news_score=_safe_float(row.get("news_score")),
        relative_volume=_safe_float(_first(row, ("relative_volume", "rel_volume"))),
        sector=_label(_first(row, ("sector", "theme", "bucket", "industry"))),
        exit_reason="unknown",
        hold_minutes=_safe_float(_first(row, ("hold_duration_minutes", "hold_minutes"))),
        route=_label(row.get("source")),
        source="catalyst_outcome",
    )


def load_research_trades(
    *,
    data_dir: Path | str,
    user_id: str,
    day: str,
    catalyst_path: Path | str | None = None,
) -> list[ResearchTrade]:
    """Load normalized research rows for one trading date."""
    attribution, _orders, _summary = load_profitability_report_inputs(data_dir=data_dir, user_id=user_id, day=day)
    trades = [_trade_from_exit(row) for row in _rows(attribution or {}, ("exits",))]
    out = [trade for trade in trades if trade is not None]
    catalyst_store = Path(catalyst_path) if catalyst_path else Path(data_dir) / "analytics" / "catalyst_outcomes.json"
    for row in load_catalyst_outcome_records(catalyst_store):
        row_user = str(row.get("user_id") or user_id)
        row_day = str(row.get("date") or row.get("observed_date") or "")
        if row_user == str(user_id) and row_day == str(day):
            trade = _trade_from_catalyst(row)
            if trade is not None:
                out.append(trade)
    return out


def _metric_row(rows: Sequence[ResearchTrade]) -> dict[str, Any]:
    returns = [row.return_pct for row in rows]
    pnls = [row.pnl for row in rows if row.pnl is not None]
    wins = [value for value in returns if value > 0.0]
    losses = [value for value in returns if value < 0.0]
    gross_gain = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "sample_count": len(rows),
        "avg_return_pct": round(sum(returns) / len(returns), 6) if returns else 0.0,
        "win_rate_pct": round((len(wins) / len(returns) * 100.0), 6) if returns else 0.0,
        "total_pnl": round(sum(pnls), 6) if pnls else 0.0,
        "avg_pnl": round(sum(pnls) / len(pnls), 6) if pnls else 0.0,
        "profit_factor": None if gross_loss == 0.0 and gross_gain > 0.0 else round(gross_gain / gross_loss, 6) if gross_loss else 0.0,
    }


def evaluate_dimension(trades: Sequence[ResearchTrade], dimension: str) -> dict[str, dict[str, Any]]:
    """Evaluate one research dimension."""
    getters = {
        "catalyst_type": lambda row: row.catalyst_type,
        "news_score": lambda row: _news_score_bin(row.news_score),
        "relative_volume": lambda row: _relative_volume_bin(row.relative_volume),
        "sector": lambda row: row.sector,
        "exit_reason": lambda row: row.exit_reason,
        "holding_period": lambda row: _hold_bin(row.hold_minutes),
    }
    if dimension not in getters:
        raise ValueError(f"unsupported research dimension {dimension!r}")
    grouped: defaultdict[str, list[ResearchTrade]] = defaultdict(list)
    for trade in trades:
        grouped[str(getters[dimension](trade))].append(trade)
    return {key: _metric_row(rows) for key, rows in sorted(grouped.items())}


def _ranked_groups(evaluations: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for dimension, groups in evaluations.items():
        for name, stats in groups.items():
            samples = int(stats.get("sample_count") or 0)
            avg_return = float(stats.get("avg_return_pct") or 0.0)
            win_rate = float(stats.get("win_rate_pct") or 0.0)
            score = round(avg_return * max(1, samples) * (0.5 + win_rate / 100.0), 6)
            rows.append({"dimension": dimension, "group": name, "score": score, **stats})
    return sorted(rows, key=lambda row: row["score"], reverse=True)


def generate_recommendations(
    *,
    evaluations: Mapping[str, Mapping[str, Mapping[str, Any]]],
    churn: Mapping[str, Any],
    replay: Mapping[str, Any] | None,
    min_samples: int = 1,
) -> list[dict[str, Any]]:
    """Generate ranked read-only strategy/config research recommendations."""
    recommendations: list[dict[str, Any]] = []
    for row in _ranked_groups(evaluations):
        samples = int(row.get("sample_count") or 0)
        if samples < min_samples or row["group"] == "unknown":
            continue
        avg_return = float(row.get("avg_return_pct") or 0.0)
        if avg_return > 0.0:
            action = "consider increasing priority"
            rationale = f"{row['dimension']}={row['group']} averaged {avg_return:.2f}% over {samples} samples."
        elif avg_return < 0.0:
            action = "consider tightening filter"
            rationale = f"{row['dimension']}={row['group']} averaged {avg_return:.2f}% over {samples} samples."
        else:
            continue
        recommendations.append(
            {
                "rank_score": abs(float(row["score"])),
                "dimension": row["dimension"],
                "group": row["group"],
                "action": action,
                "rationale": rationale,
                "config_adjustment": _config_adjustment_hint(str(row["dimension"]), str(row["group"]), avg_return),
                "auto_apply": False,
            }
        )

    weak_exits = churn.get("weak_exits") if isinstance(churn.get("weak_exits"), Mapping) else {}
    repeated = churn.get("repeated_activity") if isinstance(churn.get("repeated_activity"), Mapping) else {}
    if int(weak_exits.get("count") or 0) > 0:
        recommendations.append(
            {
                "rank_score": float(weak_exits.get("count") or 0),
                "dimension": "churn",
                "group": "weak_exits",
                "action": "review exit timing",
                "rationale": f"{int(weak_exits.get('count') or 0)} weak exits were detected.",
                "config_adjustment": "Review min-hold, stop, and signal-flip settings before changing config.",
                "auto_apply": False,
            }
        )
    repeat_count = int(repeated.get("repeated_buy_count") or 0) + int(repeated.get("repeated_sell_count") or 0)
    if repeat_count > 0:
        recommendations.append(
            {
                "rank_score": float(repeat_count),
                "dimension": "churn",
                "group": "repeated_activity",
                "action": "review cooldowns",
                "rationale": f"{repeat_count} repeated buy/sell activity signals were detected.",
                "config_adjustment": "Review symbol cooldown and replacement churn guard settings.",
                "auto_apply": False,
            }
        )

    if replay:
        replay_churn = replay.get("churn_same_day_reversal_stats")
        replay_count = int(replay_churn.get("same_day_reversal_count") or 0) if isinstance(replay_churn, Mapping) else 0
        if replay_count > 0:
            recommendations.append(
                {
                    "rank_score": float(replay_count),
                    "dimension": "replay_validation",
                    "group": "same_day_reversals",
                    "action": "validate recommendation against replay",
                    "rationale": f"Replay found {replay_count} same-day reversals; avoid loosening entries until replay churn improves.",
                    "config_adjustment": "Run replay after proposed threshold/cooldown changes before production use.",
                    "auto_apply": False,
                }
            )
    return sorted(recommendations, key=lambda row: float(row.get("rank_score") or 0.0), reverse=True)


def _config_adjustment_hint(dimension: str, group: str, avg_return: float) -> str:
    direction = "raise exposure/priority only after replay validation" if avg_return > 0 else "tighten threshold or reduce priority"
    hints = {
        "catalyst_type": f"Review catalyst type weights for {group}; {direction}.",
        "news_score": f"Review minimum news score and score buckets around {group}; {direction}.",
        "relative_volume": f"Review relative-volume gate around {group}; {direction}.",
        "sector": f"Review sector caps or allowlist priority for {group}; {direction}.",
        "exit_reason": f"Review exit handling for {group}; {direction}.",
        "holding_period": f"Review min/max hold settings around {group}; {direction}.",
    }
    return hints.get(dimension, f"Review {dimension}={group}; {direction}.")


def _date_range_ending(end_day: str, lookback_days: int) -> list[str]:
    end = date.fromisoformat(end_day)
    return [(end - timedelta(days=offset)).isoformat() for offset in range(lookback_days - 1, -1, -1)]


def build_research_feedback_report(
    *,
    data_dir: Path | str,
    user_id: str,
    day: str,
    lookback_days: int = 1,
    min_samples: int = 1,
) -> dict[str, Any]:
    """Build daily or weekly research feedback from local artifacts."""
    dates = _date_range_ending(day, max(1, int(lookback_days)))
    trades: list[ResearchTrade] = []
    daily_inputs: dict[str, Any] = {}
    for report_day in dates:
        day_trades = load_research_trades(data_dir=data_dir, user_id=user_id, day=report_day)
        trades.extend(day_trades)
        daily_inputs[report_day] = {"research_trade_count": len(day_trades)}

    attribution, orders, daily_summary = load_profitability_report_inputs(data_dir=data_dir, user_id=user_id, day=day)
    profitability = build_profitability_report(
        user_id=user_id,
        day=day,
        attribution_payload=attribution,
        order_history_payload=orders,
        daily_summary_payload=daily_summary,
    )
    churn = load_trade_churn_analysis(data_dir=data_dir, user_id=user_id, day=day)
    replay_path = discover_replay_summary_path(data_dir=data_dir, user_id=user_id, day=day)
    replay = _load_json(replay_path)
    dimensions = ("catalyst_type", "news_score", "relative_volume", "sector", "exit_reason", "holding_period")
    evaluations = {dimension: evaluate_dimension(trades, dimension) for dimension in dimensions}
    recommendations = generate_recommendations(
        evaluations=evaluations,
        churn=churn,
        replay=replay if isinstance(replay, Mapping) else None,
        min_samples=min_samples,
    )
    return {
        "version": 1,
        "date": day,
        "period": "weekly" if lookback_days > 1 else "daily",
        "lookback_days": lookback_days,
        "user_id": str(user_id or "default"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "dates": dates,
            "daily": daily_inputs,
            "trade_count": len(trades),
            "replay_summary_path": str(replay_path) if replay_path else None,
        },
        "evaluations": evaluations,
        "recommendations": recommendations,
        "profitability": profitability,
        "churn": churn,
        "replay_validation": _replay_validation_summary(replay if isinstance(replay, Mapping) else None, recommendations),
    }


def _replay_validation_summary(replay: Mapping[str, Any] | None, recommendations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not replay:
        return {"available": False, "message": "No replay summary found for validation."}
    churn = replay.get("churn_same_day_reversal_stats") if isinstance(replay.get("churn_same_day_reversal_stats"), Mapping) else {}
    route_pnl = replay.get("route_level_pnl_estimate") if isinstance(replay.get("route_level_pnl_estimate"), Mapping) else {}
    return {
        "available": True,
        "same_day_reversal_count": int(churn.get("same_day_reversal_count") or 0),
        "repeat_order_count": int(churn.get("repeat_order_count") or 0),
        "route_level_pnl_estimate": dict(route_pnl),
        "recommendations_require_replay_before_apply": len(recommendations),
    }


def write_research_feedback_outputs(
    report: Mapping[str, Any],
    *,
    project_root: Path | str,
) -> dict[str, Path]:
    """Write markdown, JSON dashboard, and HTML dashboard outputs."""
    root = Path(project_root)
    out_dir = root / "reports" / "research_feedback"
    out_dir.mkdir(parents=True, exist_ok=True)
    day = str(report.get("date") or date.today().isoformat())
    prefix = day if report.get("period") == "daily" else f"week_{day}"
    md_path = out_dir / f"{prefix}.md"
    json_path = out_dir / f"{prefix}_dashboard.json"
    html_path = out_dir / f"{prefix}_dashboard.html"
    md_path.write_text(render_research_feedback_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    html_path.write_text(render_research_feedback_dashboard_html(report), encoding="utf-8")
    return {"markdown": md_path, "dashboard_json": json_path, "dashboard_html": html_path}


def render_research_feedback_markdown(report: Mapping[str, Any]) -> str:
    """Render a research feedback report as markdown."""
    lines = [
        f"# Research Feedback {report.get('date')} [{report.get('user_id')}]",
        "",
        f"Period: {report.get('period')} ({report.get('lookback_days')} day lookback)",
        f"Trades analyzed: {((report.get('inputs') or {}).get('trade_count') if isinstance(report.get('inputs'), Mapping) else 0)}",
        "",
        "## Ranked Recommendations",
    ]
    recs = report.get("recommendations") if isinstance(report.get("recommendations"), list) else []
    if not recs:
        lines.append("- No recommendations met the sample thresholds.")
    for idx, rec in enumerate(recs[:10], start=1):
        lines.append(
            f"{idx}. **{rec.get('action')}**: {rec.get('rationale')} "
            f"Config note: {rec.get('config_adjustment')} Auto-apply: false."
        )
    lines.extend(["", "## Dimension Performance"])
    evaluations = report.get("evaluations") if isinstance(report.get("evaluations"), Mapping) else {}
    for dimension, groups in evaluations.items():
        lines.extend(["", f"### {str(dimension).replace('_', ' ').title()}", "| Group | Samples | Avg Return | Win Rate | Total PnL |", "| --- | ---: | ---: | ---: | ---: |"])
        if isinstance(groups, Mapping):
            ranked = sorted(groups.items(), key=lambda item: float(item[1].get("avg_return_pct") or 0.0), reverse=True)
            for name, stats in ranked[:8]:
                lines.append(
                    f"| {name} | {int(stats.get('sample_count') or 0)} | "
                    f"{float(stats.get('avg_return_pct') or 0.0):.2f}% | "
                    f"{float(stats.get('win_rate_pct') or 0.0):.1f}% | "
                    f"${float(stats.get('total_pnl') or 0.0):.2f} |"
                )
    replay = report.get("replay_validation") if isinstance(report.get("replay_validation"), Mapping) else {}
    lines.extend(
        [
            "",
            "## Replay Validation",
            f"- Available: {str(bool(replay.get('available'))).lower()}",
            f"- Same-day reversals: {int(replay.get('same_day_reversal_count') or 0)}",
            f"- Repeat orders: {int(replay.get('repeat_order_count') or 0)}",
            "",
            "These recommendations are research output only and do not modify trading behavior.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_research_feedback_dashboard_html(report: Mapping[str, Any]) -> str:
    """Render a small static HTML dashboard for research feedback."""
    markdown = render_research_feedback_markdown(report)
    body = (
        markdown.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>Research Feedback</title>"
        "<style>body{font-family:system-ui,Arial,sans-serif;max-width:1100px;margin:32px auto;line-height:1.45}"
        "pre{white-space:pre-wrap} table{border-collapse:collapse} td,th{border:1px solid #ddd;padding:4px 8px}</style>"
        "</head><body><pre>"
        + body
        + "</pre></body></html>\n"
    )


def resolve_research_date(*, data_dir: Path | str, user_id: str, value: str | None) -> str:
    """Resolve YYYY-MM-DD/latest/today for research feedback."""
    raw = str(value or "today").strip().lower()
    if raw == "latest":
        latest = latest_report_date(data_dir=data_dir, user_id=user_id)
        if latest is None:
            raise ValueError(f"No report artifacts found for user {user_id!r}")
        return latest
    if raw == "today":
        return date.today().isoformat()
    date.fromisoformat(raw)
    return raw
