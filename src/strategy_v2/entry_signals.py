"""Tiered long entry logic (trend + momentum vs regime score)."""
from __future__ import annotations

from typing import Any

import pandas as pd


def rsi_wilder_last(close: pd.Series, period: int = 14) -> float | None:
    """Last RSI (Wilder / RMA)."""
    if close is None or len(close) < period + 2:
        return None
    s = close.astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, 1e-12)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    out = rsi.iloc[-1]
    return float(out) if out == out else None  # NaN check


def price_breakout_last(close: pd.Series, lookback_days: int) -> bool | None:
    """True if last close > max prior *lookback_days* closes (excluding last bar)."""
    if close is None or len(close) < lookback_days + 2:
        return None
    s = close.astype(float)
    last = float(s.iloc[-1])
    window = s.iloc[-(lookback_days + 1) : -1]
    if window.empty:
        return None
    return last > float(window.max())


def allow_long_for_regime(regime_score: int) -> bool:
    """Regime permits **considering** v2 longs (still need trend / RSI per :func:`should_enter_long`)."""
    return int(regime_score) >= 2


def should_enter_long(
    *,
    regime_score: int,
    price: float,
    ma50: float,
    rsi: float | None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """
    v2 long gate:
      - score < 2: no new long
      - score >= 4: trend only (price > MA50 if required)
      - score >= 2: trend + momentum (RSI in band)
    """
    if not allow_long_for_regime(regime_score):
        return False
    v2 = (cfg or {}).get("strategy_v2") or {}
    sig = v2.get("signals") or {}
    trend = sig.get("trend") or {}
    mom = sig.get("momentum") or {}
    require_above = bool(trend.get("require_price_above_ma", True))
    trend_ok = float(price) > float(ma50) if require_above else True

    if int(regime_score) >= 4:
        return trend_ok
    if int(regime_score) >= 2:
        if rsi is None:
            return False
        lo = float(mom.get("rsi_min", 45))
        hi = float(mom.get("rsi_max", 70))
        return trend_ok and (lo < rsi < hi)
