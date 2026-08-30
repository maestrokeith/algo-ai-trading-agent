"""Breakout-specific exit decision helpers."""

from __future__ import annotations

from src.strategy import ExitReason, ExitSignal


def evaluate_breakout_exit(
    *,
    symbol: str,
    pnl_pct: float,
    price: float,
    vwap: float | None,
    ema9: float | None,
    hold_minutes: float | None,
    current_qty: int,
    partial_taken: bool,
    strong_take_profit_pct: float = 2.5,
    take_profit_pct: float = 1.5,
    stop_loss_pct: float = 0.7,
    max_hold_minutes: float = 60.0,
    trail_high: float | None = None,
    trailing_stop_pct: float | None = None,
) -> ExitSignal | None:
    """Evaluate tag-based breakout exits in priority order."""

    if pnl_pct >= strong_take_profit_pct:
        return ExitSignal(
            symbol=symbol,
            reason=ExitReason.TAKE_PROFIT,
            metadata={"ret_pct": pnl_pct, "tag": "breakout"},
        )

    if not partial_taken and pnl_pct >= take_profit_pct and current_qty > 0:
        qty_to_sell = max(1, int(current_qty * 0.5))
        if qty_to_sell < current_qty:
            return ExitSignal(
                symbol=symbol,
                reason=ExitReason.PARTIAL_TAKE_PROFIT,
                metadata={"ret_pct": pnl_pct, "qty_to_sell": qty_to_sell, "tag": "breakout"},
            )

    if (
        partial_taken
        and trail_high is not None
        and trailing_stop_pct is not None
        and trailing_stop_pct > 0
    ):
        trail_threshold = float(trail_high) * (1.0 - float(trailing_stop_pct) / 100.0)
        if price <= trail_threshold:
            return ExitSignal(
                symbol=symbol,
                reason=ExitReason.TRAILING_STOP,
                metadata={"ret_pct": pnl_pct, "tag": "breakout", "trail_high": trail_high},
            )

    if (vwap is not None and price < vwap) or (ema9 is not None and price < ema9):
        return ExitSignal(
            symbol=symbol,
            reason=ExitReason.SIGNAL_EXIT,
            metadata={"ret_pct": pnl_pct, "tag": "breakout"},
        )

    if pnl_pct <= -abs(float(stop_loss_pct)):
        return ExitSignal(
            symbol=symbol,
            reason=ExitReason.STOP_LOSS,
            metadata={"ret_pct": pnl_pct, "tag": "breakout"},
        )

    if hold_minutes is not None and hold_minutes > float(max_hold_minutes):
        return ExitSignal(
            symbol=symbol,
            reason=ExitReason.TIME_BARS,
            metadata={"hold_minutes": hold_minutes, "tag": "breakout"},
        )

    return None
