"""Performance, walk-forward and Monte-Carlo research utilities."""

from __future__ import annotations

from dataclasses import asdict
from typing import Sequence

import numpy as np
import pandas as pd

from .paper_broker import ClosedTrade


def equity_curve(trades: Sequence[ClosedTrade], initial_equity: float) -> pd.Series:
    values = [float(initial_equity)]
    for trade in trades:
        values.append(values[-1] + float(trade.pnl))
    return pd.Series(values, dtype=float)


def max_drawdown(curve: pd.Series) -> float:
    if curve.empty:
        return 0.0
    peak = curve.cummax()
    dd = (curve - peak) / peak.replace(0.0, np.nan)
    return float(abs(dd.min())) if not dd.dropna().empty else 0.0


def max_consecutive_losses(trades: Sequence[ClosedTrade]) -> int:
    max_run = run = 0
    for trade in trades:
        if trade.pnl < 0:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    return max_run


def performance_metrics(trades: Sequence[ClosedTrade], initial_equity: float) -> dict[str, float | int]:
    pnls = np.array([t.pnl for t in trades], dtype=float)
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_profit = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(abs(losses.sum())) if losses.size else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    curve = equity_curve(trades, initial_equity)
    ending = float(curve.iloc[-1]) if not curve.empty else float(initial_equity)
    return {"trades": int(len(trades)), "wins": int((pnls > 0).sum()) if pnls.size else 0, "losses": int((pnls < 0).sum()) if pnls.size else 0, "win_rate": float((pnls > 0).mean()) if pnls.size else 0.0, "profit_factor": float(profit_factor), "expectancy": float(pnls.mean()) if pnls.size else 0.0, "net_profit": float(pnls.sum()) if pnls.size else 0.0, "return_pct": (ending / initial_equity - 1.0) if initial_equity else 0.0, "max_drawdown": max_drawdown(curve), "max_consecutive_losses": max_consecutive_losses(trades)}


def grouped_statistics(trades: Sequence[ClosedTrade], by: str = "symbol") -> pd.DataFrame:
    if by not in {"symbol", "session"}:
        raise ValueError("by must be 'symbol' or 'session'")
    if not trades:
        return pd.DataFrame(columns=[by, "trades", "win_rate", "net_profit", "expectancy", "profit_factor"])
    df = pd.DataFrame([asdict(t) for t in trades])
    records = []
    for key, group in df.groupby(by, dropna=False):
        pnl = group["pnl"].astype(float)
        gp, gl = pnl[pnl > 0].sum(), abs(pnl[pnl < 0].sum())
        records.append({by: key, "trades": len(group), "win_rate": float((pnl > 0).mean()), "net_profit": float(pnl.sum()), "expectancy": float(pnl.mean()), "profit_factor": float(gp / gl) if gl > 0 else (float("inf") if gp > 0 else 0.0)})
    return pd.DataFrame(records)


def walk_forward_splits(index: pd.Index, folds: int = 5, train_fraction: float = 0.70) -> list[tuple[pd.Index, pd.Index]]:
    if folds < 1:
        raise ValueError("folds must be >= 1")
    if not 0.5 <= train_fraction < 1.0:
        raise ValueError("train_fraction must be in [0.5, 1.0)")
    n = len(index)
    if n < 20:
        return []
    block = max(1, n // (folds + 1))
    splits: list[tuple[pd.Index, pd.Index]] = []
    for i in range(1, folds + 1):
        end = min(n, block * (i + 1))
        start = max(0, end - int(block / (1 - train_fraction)))
        segment = index[start:end]
        cut = max(1, int(len(segment) * train_fraction))
        train, test = segment[:cut], segment[cut:]
        if len(test):
            splits.append((train, test))
    return splits


def monte_carlo_trade_paths(trades: Sequence[ClosedTrade], initial_equity: float, simulations: int = 1000, seed: int = 7) -> pd.DataFrame:
    if simulations < 1:
        raise ValueError("simulations must be positive")
    pnls = np.array([t.pnl for t in trades], dtype=float)
    if pnls.size == 0:
        return pd.DataFrame([{"ending_equity": initial_equity, "max_drawdown": 0.0}] * simulations)
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(simulations):
        sampled = rng.choice(pnls, size=len(pnls), replace=True)
        curve = pd.Series(np.r_[initial_equity, initial_equity + np.cumsum(sampled)])
        rows.append({"ending_equity": float(curve.iloc[-1]), "max_drawdown": max_drawdown(curve)})
    return pd.DataFrame(rows)
