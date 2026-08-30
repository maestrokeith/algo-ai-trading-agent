"""
Lean-style backtest engine: runs a QCAlgorithm over historical data.

Creates context and algorithm, calls Initialize() then OnEndOfDay() each day,
processes order requests and maintains portfolio. Returns BacktestResult
compatible with backtest.metrics.

Legacy compatibility entry point: newer backtest package code lives under
``src/backtest/``. Keep this module as a stable import surface for scripts such
as ``scripts/lean_cli.py`` until the older import path is fully retired.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Type

import pandas as pd

from ..algorithm.base import QCAlgorithm
from ..algorithm.context import AlgorithmContext, Slice, Bar, Position
from ..config_loader import load_config


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_date: date
    exit_date: date
    entry_price: float
    exit_price: float
    qty: int
    pnl: float
    pnl_pct: float
    exit_reason: str
    bars_held: int


@dataclass
class BacktestResult:
    equity_curve: list[tuple[date, float]]
    trades: list[BacktestTrade]
    initial_equity: float
    final_equity: float
    config: dict[str, Any]


class EngineBacktest:
    """
    Run backtest using an algorithm (Lean-style). Data: dict[symbol -> DataFrame] with datetime index.
    """

    def __init__(self, config: dict[str, Any] | None = None, config_path: str | None = None):
        self.config = config or load_config(config_path)

    @staticmethod
    def _timestamp_for_day(day: date) -> pd.Timestamp:
        return pd.Timestamp(day)

    @classmethod
    def _row_for_day(cls, df: pd.DataFrame, day: date) -> pd.Series | None:
        ts = cls._timestamp_for_day(day)
        if ts not in df.index:
            return None
        row = df.loc[ts]
        if isinstance(row, pd.DataFrame):
            return row.iloc[-1]
        return row

    @classmethod
    def _history_to_day(cls, df: pd.DataFrame, day: date) -> pd.DataFrame:
        return df.loc[: cls._timestamp_for_day(day)]

    def run(
        self,
        algorithm: QCAlgorithm,
        data: dict[str, pd.DataFrame],
        initial_equity: float = 100_000.0,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> BacktestResult:
        if not data:
            return BacktestResult(
                equity_curve=[],
                trades=[],
                initial_equity=initial_equity,
                final_equity=initial_equity,
                config=self.config,
            )

        all_dates: set[date] = set()
        for df in data.values():
            for d in df.index:
                all_dates.add(pd.Timestamp(d).date())
        trading_dates = sorted(all_dates)
        if start_date:
            trading_dates = [d for d in trading_dates if d >= start_date]
        if end_date:
            trading_dates = [d for d in trading_dates if d <= end_date]
        if not trading_dates:
            return BacktestResult(
                equity_curve=[],
                trades=[],
                initial_equity=initial_equity,
                final_equity=initial_equity,
                config=self.config,
            )

        context = AlgorithmContext(self.config, initial_equity)
        algorithm.config = self.config
        algorithm.initialize(context)

        if hasattr(algorithm, "_trading_dates"):
            algorithm._trading_dates = trading_dates

        cash = initial_equity
        positions: dict[str, Position] = {}
        trades: list[BacktestTrade] = []
        equity_curve: list[tuple[date, float]] = []
        date_to_idx = {d: i for i, d in enumerate(trading_dates)}

        for today in trading_dates:
            dt = datetime.combine(today, datetime.min.time())

            # Mark-to-market
            equity = cash
            for sym, pos in positions.items():
                if sym not in data:
                    continue
                row = self._row_for_day(data[sym], today)
                if row is not None:
                    equity += pos.quantity * float(row["close"])
            equity_curve.append((today, equity))

            # Build slice and history for algorithm
            bars = {}
            for sym, df in data.items():
                row = self._row_for_day(df, today)
                if row is None:
                    continue
                bars[sym] = Bar(
                    symbol=sym,
                    time=dt,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row.get("volume", 0)),
                )
            slice_obj = Slice(time=dt, bars=bars)
            context._current_bars_df = {
                sym: self._history_to_day(df, today)
                for sym, df in data.items()
                if self._row_for_day(df, today) is not None
            }

            context.set_time(dt)
            context.portfolio.set_cash(cash)
            context.portfolio.set_total_value(equity)
            context.portfolio.set_positions(positions)
            context.clear_orders()

            algorithm.on_end_of_day(context, slice_obj)

            # Process orders
            for req in context.order_requests:
                if req.get("type") != "market":
                    continue
                symbol = req["symbol"]
                qty = req["quantity"]
                stop_pct = req.get("stop_pct")
                bar = slice_obj.get(symbol)
                price = bar.close if bar else 0.0

                if qty > 0:  # Buy
                    if price <= 0 or symbol not in slice_obj.bars:
                        continue
                    cost = qty * price
                    if cost > cash:
                        continue
                    cash -= cost
                    default_stop = self.config.get("strategy", {}).get("exits", {}).get("stop_loss_pct", 1.5)
                    pos = Position(
                        symbol=symbol,
                        quantity=qty,
                        entry_price=price,
                        entry_time=dt,
                        stop_pct=stop_pct if stop_pct is not None else default_stop,
                        partial_taken=False,
                        trail_high=price,
                    )
                    positions[symbol] = pos
                else:  # Sell
                    abs_qty = abs(qty)
                    if symbol not in positions:
                        continue
                    pos = positions[symbol]
                    sell_qty = min(abs_qty, pos.quantity)
                    cash += sell_qty * price
                    if sell_qty >= pos.quantity:
                        entry_date = pos.entry_time.date() if hasattr(pos.entry_time, "date") else pd.Timestamp(pos.entry_time).date()
                        bars_held = date_to_idx.get(today, 0) - date_to_idx.get(entry_date, 0)
                        pnl = (price - pos.entry_price) * pos.quantity
                        pnl_pct = (price - pos.entry_price) / pos.entry_price * 100
                        trades.append(
                            BacktestTrade(
                                symbol=symbol,
                                side="long",
                                entry_date=entry_date,
                                exit_date=today,
                                entry_price=pos.entry_price,
                                exit_price=price,
                                qty=pos.quantity,
                                pnl=pnl,
                                pnl_pct=pnl_pct,
                                exit_reason="signal",
                                bars_held=bars_held,
                            )
                        )
                        del positions[symbol]
                    else:
                        pos.quantity -= sell_qty
                        pos.partial_taken = True
                        pos.trail_high = price

        final_equity = cash
        for sym, pos in positions.items():
            if sym not in data:
                continue
            row = self._row_for_day(data[sym], trading_dates[-1])
            if row is not None:
                final_equity += pos.quantity * float(row["close"])

        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            initial_equity=initial_equity,
            final_equity=final_equity,
            config=self.config,
        )
