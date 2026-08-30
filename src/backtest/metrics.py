"""Compute backtest performance metrics."""
from dataclasses import dataclass
from typing import Any

from .engine import BacktestResult


@dataclass
class BacktestMetrics:
    total_return_pct: float
    cagr_pct: float | None
    sharpe_ratio: float | None
    max_drawdown_pct: float
    win_rate_pct: float
    num_trades: int
    num_winners: int
    num_losers: int
    avg_trade_pnl_pct: float | None
    avg_bars_held: float | None


def compute_metrics(result: BacktestResult, risk_free_rate_annual: float = 0.0) -> BacktestMetrics:
    """
    Compute standard backtest metrics from BacktestResult.
    Sharpe uses daily equity curve; risk_free_rate_annual in decimal (e.g. 0.05 for 5%).
    """
    trades = result.trades
    curve = result.equity_curve

    if result.initial_equity <= 0:
        total_return_pct = 0.0
    else:
        total_return_pct = (result.final_equity - result.initial_equity) / result.initial_equity * 100.0

    # CAGR if we have at least 2 equity points and a span
    cagr_pct = None
    if len(curve) >= 2 and curve[0][0] != curve[-1][0]:
        from datetime import date
        start_d = curve[0][0]
        end_d = curve[-1][0]
        if hasattr(start_d, "toordinal") and hasattr(end_d, "toordinal"):
            days = (end_d - start_d).days
            if days > 0:
                years = days / 365.25
                if result.initial_equity > 0 and years > 0:
                    end_val = curve[-1][1]
                    cagr_pct = (pow(end_val / result.initial_equity, 1 / years) - 1) * 100.0

    # Max drawdown from equity curve
    max_dd_pct = 0.0
    peak = result.initial_equity
    for _, eq in curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (eq - peak) / peak * 100.0
            if dd < max_dd_pct:
                max_dd_pct = dd

    # Sharpe (annualized) from daily returns
    sharpe = None
    if len(curve) >= 2:
        import numpy as np
        eq_vals = [result.initial_equity] + [e[1] for e in curve]
        returns = []
        for i in range(1, len(eq_vals)):
            if eq_vals[i - 1] > 0:
                returns.append((eq_vals[i] - eq_vals[i - 1]) / eq_vals[i - 1])
        if returns:
            rf_daily = risk_free_rate_annual / 252.0
            excess = [r - rf_daily for r in returns]
            if len(excess) > 0 and (sum(1 for e in excess if e != 0) or True):
                std = np.std(excess)
                if std and std > 0:
                    sharpe = (np.mean(excess) / std) * (252 ** 0.5)

    # Trade stats
    num_trades = len(trades)
    winners = [t for t in trades if t.pnl > 0]
    losers = [t for t in trades if t.pnl <= 0]
    win_rate_pct = (len(winners) / num_trades * 100.0) if num_trades > 0 else 0.0
    avg_pnl_pct = (sum(t.pnl_pct for t in trades) / num_trades) if num_trades else None
    avg_bars = (sum(t.bars_held for t in trades) / num_trades) if num_trades else None

    return BacktestMetrics(
        total_return_pct=total_return_pct,
        cagr_pct=cagr_pct,
        sharpe_ratio=sharpe,
        max_drawdown_pct=max_dd_pct,
        win_rate_pct=win_rate_pct,
        num_trades=num_trades,
        num_winners=len(winners),
        num_losers=len(losers),
        avg_trade_pnl_pct=avg_pnl_pct,
        avg_bars_held=avg_bars,
    )
