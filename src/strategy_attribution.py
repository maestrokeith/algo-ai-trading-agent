"""Strategy attribution dashboard data for live and historical trades."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.exposure import ETF_SYMBOLS
from src.options_premium_risk import is_option_symbol

ATTRIBUTION_BUCKETS = ("core", "dynamic", "options", "news-driven", "ETF fallback")


@dataclass(frozen=True)
class StrategyAttributionRow:
    """P/L attribution for one strategy bucket."""

    bucket: str
    trades: int
    wins: int
    losses: int
    realized_pnl: float
    win_rate: float
    avg_pnl: float

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-serializable row."""
        return self.__dict__.copy()


def _as_float(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value != value:
        return 0.0
    return value


def classify_trade_bucket(trade: Mapping[str, Any]) -> str:
    """Classify a trade into one dashboard attribution bucket."""
    strategy = str(trade.get("strategy") or trade.get("source") or "").strip().lower()
    symbol = str(trade.get("symbol") or "").strip().upper()
    catalyst = str(trade.get("catalyst_type") or "").strip().lower()
    route = str(trade.get("route") or trade.get("bucket") or "").strip().lower()
    if "news" in strategy or catalyst or route == "news":
        return "news-driven"
    if "option" in strategy or is_option_symbol(symbol) or str(trade.get("asset_class") or "").lower() == "option":
        return "options"
    if "etf" in strategy or route == "etf_fallback" or symbol in ETF_SYMBOLS:
        return "ETF fallback"
    if "dynamic" in strategy:
        return "dynamic"
    return "core"


def build_strategy_attribution(trades: Sequence[Mapping[str, Any]]) -> list[StrategyAttributionRow]:
    """Aggregate realized P/L separately for each strategy bucket."""
    rows: dict[str, dict[str, float | int]] = {
        bucket: {"trades": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0}
        for bucket in ATTRIBUTION_BUCKETS
    }
    for trade in trades:
        bucket = classify_trade_bucket(trade)
        pnl = _as_float(trade.get("pnl"))
        row = rows[bucket]
        row["trades"] = int(row["trades"]) + 1
        row["realized_pnl"] = float(row["realized_pnl"]) + pnl
        if pnl > 0:
            row["wins"] = int(row["wins"]) + 1
        elif pnl < 0:
            row["losses"] = int(row["losses"]) + 1

    out: list[StrategyAttributionRow] = []
    for bucket in ATTRIBUTION_BUCKETS:
        row = rows[bucket]
        trades_count = int(row["trades"])
        wins = int(row["wins"])
        losses = int(row["losses"])
        pnl = float(row["realized_pnl"])
        win_rate = wins / trades_count if trades_count else 0.0
        avg_pnl = pnl / trades_count if trades_count else 0.0
        out.append(
            StrategyAttributionRow(
                bucket=bucket,
                trades=trades_count,
                wins=wins,
                losses=losses,
                realized_pnl=pnl,
                win_rate=win_rate,
                avg_pnl=avg_pnl,
            )
        )
    return out


def strategy_attribution_dashboard(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return dashboard-ready attribution payload."""
    rows = build_strategy_attribution(trades)
    total_pnl = sum(row.realized_pnl for row in rows)
    total_trades = sum(row.trades for row in rows)
    return {
        "total_pnl": total_pnl,
        "total_trades": total_trades,
        "rows": [row.as_dict() for row in rows],
    }


__all__ = [
    "ATTRIBUTION_BUCKETS",
    "StrategyAttributionRow",
    "build_strategy_attribution",
    "classify_trade_bucket",
    "strategy_attribution_dashboard",
]
