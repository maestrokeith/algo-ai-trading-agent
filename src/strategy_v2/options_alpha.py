"""
Options alpha independent of stock sleeve: realized-vol proxy + price breakout.

True IV rank needs an options chain; we use **realized volatility rank** (0–100)
over ``close`` history as a practical stand-in when ``use_realized_vol_as_iv_proxy``.
"""
from __future__ import annotations

from typing import Any

import pandas as pd


def check_iv_rank_proxy(close: pd.Series, cfg: dict[str, Any] | None = None) -> float | None:
    """
    Percentile rank of the latest 20D realized vol vs trailing ~1y of 20D vols.
    Returns None if insufficient history. Interprete like IV rank for thresholds.
    """
    v2 = (cfg or {}).get("strategy_v2") or {}
    o = v2.get("options") or {}
    if not bool(o.get("use_realized_vol_as_iv_proxy", True)):
        return 50.0
    if close is None or len(close) < 80:
        return None
    s = close.astype(float)
    rets = s.pct_change().dropna()
    if len(rets) < 60:
        return None
    rv_series = rets.rolling(20).std() * (252.0 ** 0.5)
    rv_now = rv_series.iloc[-1]
    hist = rv_series.dropna().iloc[-252:]
    if hist.empty or not (rv_now == rv_now):
        return None
    rank = float((hist < rv_now).sum()) / float(len(hist)) * 100.0
    return rank


def price_breakout_for_options(close: pd.Series, lookback: int) -> bool | None:
    if close is None or len(close) < lookback + 2:
        return None
    last = float(close.iloc[-1])
    prior = close.iloc[-(lookback + 1) : -1].astype(float)
    return last > float(prior.max())


def options_signal_independent(
    symbol: str,
    *,
    df: pd.DataFrame,
    cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """
    ``iv_spike`` (proxy) AND ``breakout`` when both data paths are available.

    Returns (allowed, reason_tag).
    """
    _ = symbol
    v2 = (cfg or {}).get("strategy_v2") or {}
    o = v2.get("options") or {}
    if not bool(o.get("enabled", False)) or not bool(o.get("independent_signal", False)):
        return False, "v2 options independent_signal off"
    if df is None or df.empty or "close" not in df.columns:
        return False, "no bars"
    close = df["close"]
    thr = float(o.get("iv_rank_proxy_min", 60))
    rk = check_iv_rank_proxy(close, cfg)
    if rk is None:
        return False, "iv_rank_proxy unavailable"
    iv_spike = rk > thr
    lb = int(o.get("breakout_lookback_days", 5))
    br = price_breakout_for_options(close, lb)
    if br is None:
        return False, "breakout unavailable"
    if iv_spike and br:
        return True, "iv_rank=%.0f breakout=%dd" % (rk, lb)
    return False, "iv_rank=%.0f (need>%g) breakout=%s" % (rk, thr, br)


def options_signal(
    symbol: str,
    *,
    df: pd.DataFrame,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """
    Boolean v2 gate for independent options entries.

    Equivalent to ``options_signal_independent(...)[0]``. Use
    ``options_signal_independent`` when you need the reason string.

    Typical flow::

        if options_signal(symbol, df=df, cfg=config):
            trade_options(symbol, config=config, ...)
    """
    return options_signal_independent(symbol, df=df, cfg=cfg)[0]
