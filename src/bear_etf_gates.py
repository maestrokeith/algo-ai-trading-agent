"""
Bear / inverse ETF entry helpers (daily bars).

``daily_fresh_cross_below_ma`` detects a *new* breakdown: prior close at/above the
rolling MA and latest close below — stricter than “already trading below the MA”.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


def daily_fresh_cross_below_ma(df: "pd.DataFrame", ma_period: int) -> bool | None:
    """
    Return True if the most recent completed bar crosses *down* through the MA
    (prev close >= prev MA, current close < current MA).

    Returns None if there is insufficient history or missing columns.
    """
    if df is None or getattr(df, "empty", True):
        return None
    if "close" not in df.columns:
        return None
    need = int(ma_period) + 2
    if len(df) < need:
        return None
    close = df["close"]
    ma = close.rolling(int(ma_period)).mean()
    try:
        c_prev = float(close.iloc[-2])
        c_curr = float(close.iloc[-1])
        m_prev = float(ma.iloc[-2])
        m_curr = float(ma.iloc[-1])
    except (TypeError, ValueError):
        return None

    if any(math.isnan(x) for x in (c_prev, c_curr, m_prev, m_curr)):
        return None
    return c_prev >= m_prev and c_curr < m_curr
