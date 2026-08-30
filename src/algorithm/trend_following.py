"""
Default algorithm: trend-following with pullback, volatility filter, and full exit rules.

Uses strategy + sizer from config; implements OnEndOfDay to check exits then entries
and submit orders via context (Lean-style).
"""
from datetime import datetime
from typing import Any

import pandas as pd

from .base import QCAlgorithm
from .context import AlgorithmContext, Slice, Bar, Position

# Use existing strategy and sizer
from ..profit_reentry_gate import entries_reentry_pullback_cfg, profit_reentry_price_allowed
from ..strategy import TrendFollowingStrategy, ExitReason, _atr
from ..position_sizing import PositionSizer


class TrendFollowingAlgorithm(QCAlgorithm):
    """
    Trend-following: price above slow MA, pullback/momentum, volatility filter.
    Exits: stop-loss, time, partial take-profit, trailing stop. Cooldown/breakout after stop or profit.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self._strategy = TrendFollowingStrategy(self.config)
        self._sizer = PositionSizer(self.config)
        self._last_stop_loss_at: dict[str, datetime] = {}
        self._last_stopped_ref_price: dict[str, float] = {}
        self._last_profit_exit_at: dict[str, datetime] = {}
        self._last_profit_exit_price: dict[str, float] = {}
        self._last_profit_exit_was_partial: dict[str, bool] = {}
        self._trading_dates: list | None = None  # Set by engine for bars_held

    def initialize(self, context: AlgorithmContext) -> None:
        """Universe and parameters come from config; no extra setup needed."""
        pass

    def on_end_of_day(self, context: AlgorithmContext, slice: Slice) -> None:
        dt = context.time
        spread_pct = 0.15
        equity = context.portfolio.total_portfolio_value
        positions = context.portfolio.positions()
        data_df = getattr(context, "_current_bars_df", None)  # Engine attaches full history for the day

        # ---- Exits ----
        for symbol, pos in list(positions.items()):
            bar = slice.get(symbol)
            if not bar:
                continue
            bars_held = self._bars_held(pos.entry_time, dt)
            atr_pct = self._atr_pct_for_symbol(context, symbol, bar.close) if data_df and symbol in data_df else None
            try:
                wall_mins = max(0.0, (dt - pos.entry_time).total_seconds() / 60.0)
            except Exception:
                wall_mins = None

            exit_signal = self._strategy.check_exit(
                symbol,
                pos.entry_price,
                bar.close,
                bars_held,
                spread_pct=spread_pct,
                atr_pct=atr_pct,
                partial_taken=pos.partial_taken,
                trail_high=pos.trail_high or pos.entry_price,
                current_qty=pos.quantity,
                minutes_held=wall_mins,
                smart_scale_out_index=int(getattr(pos, "smart_scale_out_index", 0) or 0),
            )

            if exit_signal is None:
                continue

            if exit_signal.reason in (ExitReason.PARTIAL_TAKE_PROFIT, ExitReason.KILL_SWITCH_PARTIAL):
                qty_to_sell = exit_signal.metadata.get("qty_to_sell", max(1, pos.quantity // 2))
                qty_to_sell = min(qty_to_sell, pos.quantity)
                context.market_order(symbol, -qty_to_sell)
                self._last_profit_exit_at[symbol] = dt
                self._last_profit_exit_price[symbol] = bar.close
                self._last_profit_exit_was_partial[symbol] = True
                _lvl = exit_signal.metadata.get("smart_scale_level")
                if _lvl is not None:
                    pos.smart_scale_out_index = int(_lvl) + 1
                # Engine will update position (reduce qty, set partial_taken, trail_high)
                continue

            context.market_order(symbol, -pos.quantity)
            if exit_signal.reason == ExitReason.STOP_LOSS:
                self._last_stop_loss_at[symbol] = dt
                self._last_stopped_ref_price[symbol] = pos.entry_price
            elif exit_signal.reason in (ExitReason.TAKE_PROFIT, ExitReason.TRAILING_STOP):
                self._last_profit_exit_at[symbol] = dt
                self._last_profit_exit_price[symbol] = bar.close
                self._last_profit_exit_was_partial[symbol] = False

        # ---- Entries (need full history per symbol) ----
        if data_df is None:
            return
        current_positions_for_sizer = {}
        for sym, pos in context.portfolio.positions().items():
            bar = slice.get(sym)
            if bar:
                current_positions_for_sizer[sym] = {"notional": pos.quantity * bar.close, "stop_pct": pos.stop_pct}

        for symbol in data_df.keys():
            if symbol in context.portfolio.positions():
                continue
            df_sym = data_df.get(symbol)
            if df_sym is None or len(df_sym) < self._strategy.min_history_bars_for_entry(symbol):
                continue
            if len(df_sym) == 0 or pd.Timestamp(df_sym.index[-1]).date() != dt.date():
                continue

            # Cooldown / breakout
            bypass_cd = self._strategy.strong_trend_reconfirm_ok(symbol, df_sym, None)
            if not bypass_cd and symbol in self._last_stop_loss_at:
                elapsed_min = (dt - self._last_stop_loss_at[symbol]).total_seconds() / 60.0
                if elapsed_min < self._strategy.cooldown_after_stop_minutes:
                    continue
            if self._strategy.require_new_breakout_after_stop and symbol in self._last_stopped_ref_price:
                close_now = float(df_sym["close"].iloc[-1])
                if close_now <= self._last_stopped_ref_price[symbol]:
                    continue
            if not bypass_cd and symbol in self._last_profit_exit_at:
                elapsed_min = (dt - self._last_profit_exit_at[symbol]).total_seconds() / 60.0
                _pcd = (
                    float(self._strategy.cooldown_after_profit_partial_minutes)
                    if self._last_profit_exit_was_partial.get(symbol)
                    else float(self._strategy.cooldown_after_profit_minutes)
                )
                if elapsed_min < _pcd:
                    continue
            _allow_pb, _buf_pct = entries_reentry_pullback_cfg(self.config)
            if symbol in self._last_profit_exit_price and (
                self._strategy.require_price_above_exit_after_profit or _allow_pb
            ):
                close_now = float(df_sym["close"].iloc[-1])
                _ok_px, _ = profit_reentry_price_allowed(
                    close_now,
                    self._last_profit_exit_price[symbol],
                    require_price_above_exit_after_profit=self._strategy.require_price_above_exit_after_profit,
                    allow_reentry_on_pullback=_allow_pb,
                    reentry_price_buffer_pct=_buf_pct,
                )
                if not _ok_px:
                    continue

            atr_pct = None
            if len(df_sym) >= self._strategy.atr_period:
                atr_ser = self._strategy.atr_pct(df_sym)
                if len(atr_ser) and not pd.isna(atr_ser.iloc[-1]):
                    atr_pct = float(atr_ser.iloc[-1])

            entry_signal = self._strategy.generate_entry(symbol, df_sym, spread_pct, atr_pct)
            if entry_signal is None:
                continue

            price = float(df_sym["close"].iloc[-1])
            sizing = self._sizer.size_position(
                account_equity=equity,
                price=price,
                stop_distance_pct=entry_signal.stop_pct,
                symbol=symbol,
                current_positions=current_positions_for_sizer,
                sector_exposure_pct={},
                symbol_sector=None,
                atr_pct=atr_pct,
                regime_size_multiplier=None,
                conviction_score=entry_signal.strength,
                ohlcv_df=df_sym,
            )
            if sizing.reject_reason or sizing.shares <= 0:
                continue
            current_with_stops = [(p.get("notional", 0), p.get("stop_pct", 0)) for p in current_positions_for_sizer.values()]
            open_risk_pct = self._sizer.total_open_risk_pct(equity, current_with_stops)
            if self._sizer.would_exceed_max_open_risk(open_risk_pct, sizing.risk_pct):
                continue

            context.market_order(symbol, sizing.shares, stop_pct=entry_signal.stop_pct)
            current_positions_for_sizer[symbol] = {"notional": sizing.shares * price, "stop_pct": entry_signal.stop_pct}

            self._last_stop_loss_at.pop(symbol, None)
            self._last_stopped_ref_price.pop(symbol, None)
            self._last_profit_exit_at.pop(symbol, None)
            self._last_profit_exit_price.pop(symbol, None)
            self._last_profit_exit_was_partial.pop(symbol, None)

    def _bars_held(self, entry_time: datetime, current: datetime) -> int:
        if self._trading_dates is None:
            return max(0, (current - entry_time).days)
        try:
            entry_idx = next(i for i, d in enumerate(self._trading_dates) if d >= entry_time.date())
            curr_idx = next(i for i, d in enumerate(self._trading_dates) if d >= current.date())
            return max(0, curr_idx - entry_idx)
        except StopIteration:
            return max(0, (current - entry_time).days)

    def _atr_pct_for_symbol(self, context: AlgorithmContext, symbol: str, close: float) -> float | None:
        data_df = getattr(context, "_current_bars_df", None)
        if not data_df or symbol not in data_df:
            return None
        df = data_df[symbol]
        if len(df) < 14:
            return None
        atr = _atr(df["high"], df["low"], df["close"], 14)
        if len(atr) and atr.iloc[-1]:
            return float((atr.iloc[-1] / close) * 100)
        return None
