"""Daily trade postmortem analysis for automated strategy review."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class TradePostmortem:
    """AI-style explanation for one completed trade."""

    symbol: str
    outcome: str
    pnl: float
    return_pct: float | None
    strategy: str
    explanation: str


@dataclass(frozen=True)
class DailyPostmortem:
    """Daily automated review with winner/loser explanations and suggestions."""

    winners: list[TradePostmortem]
    losers: list[TradePostmortem]
    suggestions: list[str]
    metrics: dict[str, float | str | None] | None = None
    missed_winners: list[dict[str, Any]] | None = None
    avoidable_losers: list[dict[str, Any]] | None = None


def _float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trade_return_pct(trade: Mapping[str, Any]) -> float | None:
    for key in ("return_pct", "pnl_pct", "realized_return_pct", "profit_loss_pct"):
        value = _float_or_none(trade.get(key))
        if value is not None:
            return value
    pnl = _float_or_none(trade.get("pnl"))
    qty = _float_or_none(trade.get("qty"))
    price = _float_or_none(trade.get("filled_avg_price"))
    if pnl is None or qty is None or price is None:
        return None
    notional = abs(qty * price)
    if notional <= 0:
        return None
    return (pnl / notional) * 100.0


def _first_value(trade: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = trade.get(key)
        if value is not None and str(value).strip() != "":
            return value
    return None


def _hold_minutes(trade: Mapping[str, Any]) -> float | None:
    direct = _float_or_none(_first_value(trade, ("hold_minutes", "duration_minutes", "hold_duration_minutes")))
    if direct is not None:
        return direct
    hours = _float_or_none(_first_value(trade, ("hold_hours", "duration_hours")))
    if hours is not None:
        return hours * 60.0
    return None


def _entry_rationale_parts(trade: Mapping[str, Any]) -> list[str]:
    fields = [
        ("catalyst type", _first_value(trade, ("catalyst_type", "entry_catalyst_type"))),
        ("news score", _first_value(trade, ("news_score", "entry_news_score"))),
        ("catalyst score", _first_value(trade, ("catalyst_score", "entry_catalyst_score"))),
        ("event score", _first_value(trade, ("event_score", "entry_event_score"))),
        ("relative volume", _first_value(trade, ("relative_volume", "rel_volume", "entry_relative_volume"))),
        ("momentum", _first_value(trade, ("momentum_score", "momentum", "strength_eff", "entry_strength"))),
        ("regime score", _first_value(trade, ("regime_score", "entry_regime_score"))),
    ]
    return [f"{label}: {value}" for label, value in fields if value is not None]


def _exit_rationale(trade: Mapping[str, Any]) -> str:
    raw = str(_first_value(trade, ("exit_reason", "reason", "sell_reason", "exit_type")) or "").strip().lower()
    checks = [
        ("stop_loss", "stop loss"),
        ("stop loss", "stop loss"),
        ("profit_target", "profit target"),
        ("take_profit", "profit target"),
        ("time_exit", "time exit"),
        ("time", "time exit"),
        ("exposure_reduction", "exposure reduction"),
        ("exposure", "exposure reduction"),
        ("allocator_rebalance", "allocator rebalance"),
        ("rebalance", "allocator rebalance"),
        ("manual", "manual exit"),
    ]
    for needle, label in checks:
        if needle in raw:
            return label
    return raw.replace("_", " ") if raw else "not recorded"


def _daily_metrics(trades: Sequence[Mapping[str, Any]]) -> dict[str, float | str | None]:
    realized = [trade for trade in trades if _float_or_none(trade.get("pnl")) is not None]
    pnls = [float(_float_or_none(trade.get("pnl")) or 0.0) for trade in realized]
    returns = [_trade_return_pct(trade) for trade in realized]
    winners = [pnl for pnl in pnls if pnl > 0]
    losers = [pnl for pnl in pnls if pnl < 0]
    hold_times = [value for value in (_hold_minutes(trade) for trade in realized) if value is not None]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    return {
        "trade_count": float(len(realized)),
        "win_rate_pct": (len(winners) / len(realized) * 100.0) if realized else 0.0,
        "average_winner": mean(winners) if winners else 0.0,
        "average_loser": mean(losers) if losers else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit <= 0 else float("inf")),
        "average_hold_minutes": mean(hold_times) if hold_times else None,
        "average_return_pct": mean([ret for ret in returns if ret is not None]) if any(ret is not None for ret in returns) else None,
    }


def _missed_winners(trades: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in trades:
        missed = _float_or_none(_first_value(trade, ("missed_return_pct", "max_favorable_excursion_pct")))
        realized = _trade_return_pct(trade) or 0.0
        if missed is None or missed <= max(0.0, realized):
            continue
        rows.append({"symbol": str(trade.get("symbol") or "").upper(), "missed_return_pct": missed, "realized_return_pct": realized})
    return sorted(rows, key=lambda row: float(row["missed_return_pct"]), reverse=True)[:5]


def _avoidable_losers(trades: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trade in trades:
        pnl = _float_or_none(trade.get("pnl")) or 0.0
        if pnl >= 0:
            continue
        avoidable = bool(trade.get("avoidable_loss")) or _exit_rationale(trade) in {"manual exit", "not recorded"}
        low_news = (_float_or_none(trade.get("news_score")) or 0.0) < 4.0
        weak_rvol = (_float_or_none(_first_value(trade, ("relative_volume", "rel_volume"))) or 0.0) < 1.0
        if avoidable or low_news or weak_rvol:
            rows.append({"symbol": str(trade.get("symbol") or "").upper(), "pnl": pnl, "reason": _exit_rationale(trade)})
    return sorted(rows, key=lambda row: float(row["pnl"]))[:5]


def explain_trade(trade: Mapping[str, Any]) -> TradePostmortem | None:
    """Explain a single realized trade using available strategy and catalyst metadata."""
    symbol = str(trade.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    pnl = _float_or_none(trade.get("pnl")) or 0.0
    if pnl == 0.0:
        return None
    strategy = str(trade.get("strategy") or trade.get("source") or "unknown").strip()
    return_pct = _trade_return_pct(trade)
    catalyst = str(trade.get("catalyst_type") or "").strip().lower()
    news_score = _float_or_none(trade.get("news_score"))
    outcome = "winner" if pnl > 0 else "loser"
    parts: list[str] = []
    if "dynamic" in strategy.lower():
        parts.append("dynamic candidate")
    else:
        parts.append(strategy or "unclassified strategy")
    if catalyst:
        parts.append(f"{catalyst} catalyst")
    entry_parts = _entry_rationale_parts(trade)
    if entry_parts:
        parts.append("entry rationale (" + "; ".join(entry_parts) + ")")
    parts.append(f"exit rationale: {_exit_rationale(trade)}")
    if news_score is not None and news_score >= 7:
        parts.append("strong news score")
    elif news_score is not None and news_score > 0:
        parts.append("moderate news score")
    if return_pct is not None:
        parts.append(f"{return_pct:.2f}% realized return")
    explanation = f"{symbol} was a {outcome}: " + ", ".join(parts) + "."
    return TradePostmortem(
        symbol=symbol,
        outcome=outcome,
        pnl=float(pnl),
        return_pct=return_pct,
        strategy=strategy,
        explanation=explanation,
    )


def build_daily_postmortem(trades: Sequence[Mapping[str, Any]]) -> DailyPostmortem:
    """Explain daily winners/losers and suggest parameter improvements."""
    explanations = [item for item in (explain_trade(trade) for trade in trades) if item is not None]
    winners = sorted(
        [item for item in explanations if item.pnl > 0],
        key=lambda item: item.pnl,
        reverse=True,
    )
    losers = sorted([item for item in explanations if item.pnl < 0], key=lambda item: item.pnl)
    dynamic = [
        trade
        for trade in trades
        if "dynamic" in str(trade.get("strategy") or trade.get("source") or "").lower()
    ]
    suggestions: list[str] = []
    if dynamic:
        dynamic_losses = [trade for trade in dynamic if (_float_or_none(trade.get("pnl")) or 0.0) < 0]
        if len(dynamic_losses) / len(dynamic) >= 0.5:
            suggestions.append("Review dynamic entry thresholds; at least half of dynamic trades lost money today.")
        low_news_losses = [
            trade
            for trade in dynamic_losses
            if (_float_or_none(trade.get("news_score")) or 0.0) < 7
        ]
        if low_news_losses:
            suggestions.append("Consider requiring stronger news scores or smaller starter size for weak-catalyst dynamic entries.")
    if losers and not winners:
        suggestions.append("No winning exits were recorded; review stop placement and reduce next-session risk until signals improve.")
    if winners and losers:
        best = winners[0]
        worst = losers[0]
        if abs(worst.pnl) > best.pnl:
            suggestions.append("Largest loser exceeded largest winner; tighten loss caps or scale winners more gradually.")
    if not suggestions:
        suggestions.append("No parameter changes suggested from today's trade set.")
    return DailyPostmortem(
        winners=winners[:5],
        losers=losers[:5],
        suggestions=suggestions,
        metrics=_daily_metrics(trades),
        missed_winners=_missed_winners(trades),
        avoidable_losers=_avoidable_losers(trades),
    )


def _fmt_money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def render_daily_postmortem_markdown(
    trades: Sequence[Mapping[str, Any]],
    *,
    report_date: date | str,
    user_id: str | None = None,
) -> str:
    """Render the daily trade postmortem markdown report."""
    review = build_daily_postmortem(trades)
    metrics = review.metrics or {}
    pf = metrics.get("profit_factor")
    profit_factor = "inf" if pf == float("inf") else ("n/a" if pf is None else f"{float(pf):.2f}")
    avg_hold = metrics.get("average_hold_minutes")
    lines = [
        f"# Trade Postmortem - {report_date}",
        "",
        f"User: {user_id or 'default'}",
        "",
        "## Summary",
        "",
        f"- Trades: {int(float(metrics.get('trade_count') or 0))}",
        f"- Win rate: {_fmt_pct(float(metrics.get('win_rate_pct') or 0.0))}",
        f"- Average winner: {_fmt_money(float(metrics.get('average_winner') or 0.0))}",
        f"- Average loser: {_fmt_money(float(metrics.get('average_loser') or 0.0))}",
        f"- Profit factor: {profit_factor}",
        f"- Average hold time: {'n/a' if avg_hold is None else f'{float(avg_hold):.1f} minutes'}",
        "",
        "## Winners",
        "",
    ]
    if review.winners:
        lines.extend(f"- {row.explanation}" for row in review.winners)
    else:
        lines.append("- No winning realized trades.")
    lines.extend(["", "## Losers", ""])
    if review.losers:
        lines.extend(f"- {row.explanation}" for row in review.losers)
    else:
        lines.append("- No losing realized trades.")
    lines.extend(["", "## Biggest Missed Winners", ""])
    if review.missed_winners:
        lines.extend(
            f"- {row['symbol']}: missed {_fmt_pct(float(row['missed_return_pct']))}, realized {_fmt_pct(float(row['realized_return_pct']))}"
            for row in review.missed_winners
        )
    else:
        lines.append("- No missed winner data recorded.")
    lines.extend(["", "## Biggest Avoidable Losers", ""])
    if review.avoidable_losers:
        lines.extend(
            f"- {row['symbol']}: {_fmt_money(float(row['pnl']))}, exit rationale: {row['reason']}"
            for row in review.avoidable_losers
        )
    else:
        lines.append("- No avoidable loser candidates detected.")
    lines.extend(["", "## Parameter Review", ""])
    lines.extend(f"- {item}" for item in review.suggestions)
    lines.append("")
    return "\n".join(lines)


def write_daily_postmortem_report(
    trades: Sequence[Mapping[str, Any]],
    *,
    report_date: date | str,
    reports_dir: str | Path = "reports/trade_postmortem",
    user_id: str | None = None,
) -> Path:
    """Write ``reports/trade_postmortem/YYYY-MM-DD.md`` and return the path."""
    out_dir = Path(reports_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{report_date}.md"
    out.write_text(
        render_daily_postmortem_markdown(trades, report_date=report_date, user_id=user_id),
        encoding="utf-8",
    )
    return out


__all__ = [
    "DailyPostmortem",
    "TradePostmortem",
    "build_daily_postmortem",
    "explain_trade",
    "render_daily_postmortem_markdown",
    "write_daily_postmortem_report",
]
