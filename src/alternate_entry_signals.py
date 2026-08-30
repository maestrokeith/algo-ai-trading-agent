"""
Optional live-loop entry styles when the trend MA prefilter fails.

Used by ``run_alpaca_loop`` with ``strategy.alternate_entries`` — each style is
independent; the first match in ``order`` wins. Matches become
:class:`~src.strategy.EntrySignal` overrides so :meth:`TradingEngine.run_entry_gates`
does not call trend-only :meth:`TrendFollowingStrategy.generate_entry`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import AbstractSet, Any, Mapping, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlternateEntryMatch:
    """Which alternate style fired (metadata ``source`` for routing / logs)."""

    kind: str  # "breakout" | "mean_reversion" | "volatility"


def _enum_value_or_self(x: Any) -> Any:
    """Accept enum-like values or plain scalars interchangeably."""
    return getattr(x, "value", x)


def _rsi_last(close: pd.Series, period: int) -> float | None:
    if close is None or len(close) < period + 2:
        return None
    delta = close.astype(float).diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / float(period), adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / float(period), adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    v = float(rsi.iloc[-1])
    if v != v:
        return None
    return v


def _subcfg(root: Mapping[str, Any], key: str) -> dict[str, Any]:
    raw = root.get(_enum_value_or_self(key))
    return dict(raw) if isinstance(raw, dict) else {}


def _breakout_ok(df: pd.DataFrame, cfg: Mapping[str, Any]) -> bool:
    if not bool(cfg.get("enabled", False)):
        return False
    need = int(cfg.get("lookback", 20)) + 1
    if df is None or df.empty or len(df) < need or "high" not in df.columns or "close" not in df.columns:
        return False
    lookback = max(2, int(cfg.get("lookback", 20)))
    high = df["high"].astype(float)
    close = df["close"].astype(float)
    prior = high.iloc[-(lookback + 1) : -1]
    if prior.empty:
        return False
    prior_max = float(prior.max())
    if prior_max <= 0:
        return False
    eps = float(cfg.get("breakout_epsilon_pct", 0.0)) / 100.0
    last = float(close.iloc[-1])
    return last >= prior_max * (1.0 - eps)


def _mean_reversion_ok(df: pd.DataFrame, cfg: Mapping[str, Any]) -> bool:
    if not bool(cfg.get("enabled", False)):
        return False
    rsi_period = max(2, int(cfg.get("rsi_period", 14)))
    # Floor 5 so short histories / tests can use a small structural MA; default YAML uses 200.
    ma_long = max(5, int(cfg.get("ma_long", 200)))
    need = max(rsi_period + 2, ma_long + 1)
    if df is None or df.empty or len(df) < need or "close" not in df.columns:
        return False
    close = df["close"].astype(float)
    rsi_max = float(cfg.get("rsi_max", 38.0))
    rsi = _rsi_last(close, rsi_period)
    if rsi is None or rsi >= rsi_max:
        return False
    ma200 = float(close.rolling(ma_long).mean().iloc[-1])
    last = float(close.iloc[-1])
    if ma200 != ma200 or last <= ma200:
        return False
    return True


def _volatility_ok(df: pd.DataFrame, cfg: Mapping[str, Any], *, atr_pct: float | None) -> bool:
    if not bool(cfg.get("enabled", False)):
        return False
    if atr_pct is None or atr_pct != atr_pct:
        return False
    lo = float(cfg.get("atr_pct_min", 1.25))
    hi = float(cfg.get("atr_pct_max", 8.0))
    if not (lo <= float(atr_pct) <= hi):
        return False
    if df is None or df.empty or not all(c in df.columns for c in ("high", "low", "close")):
        return False
    hi_b = float(df["high"].astype(float).iloc[-1])
    lo_b = float(df["low"].astype(float).iloc[-1])
    cl = float(df["close"].astype(float).iloc[-1])
    rng = hi_b - lo_b
    if rng <= 0:
        return False
    frac = float(cfg.get("daily_range_close_frac", 0.55))
    pos = (cl - lo_b) / rng
    return pos >= frac


def evaluate_alternate_entries(
    df: pd.DataFrame,
    config: Mapping[str, Any],
    *,
    trend_long_ok: bool,
    regime_score: int | None,
    atr_pct: float | None,
    symbol_upper: str | None = None,
    breakout_intraday_allowlist: AbstractSet[str] | None = None,
    skip_breakout_intraday_prefilter: bool = False,
) -> AlternateEntryMatch | None:
    """
    Return the first alternate style that passes, or ``None``.

    * *breakout* — close breaks above the max of prior *lookback* highs (excluding today).
    * *mean_reversion* — RSI oversold vs ``rsi_max`` while price stays above long MA.
    * *volatility* — ATR% in band and close in the upper fraction of the day's range.

    Skips when ``trend_long_ok`` (trend path handles the symbol) or regime is below
    ``min_regime_score``.

    When ``breakout_intraday_allowlist`` is not ``None``, the *breakout* style only runs if
    ``symbol_upper`` is in that set (intraday sector/top breakout prefilter from the live loop),
    unless ``skip_breakout_intraday_prefilter`` is true (e.g. scanner-added dynamic symbols).
    ``None`` preserves legacy behavior: no intraday allowlist gate.
    """
    if trend_long_ok:
        return None
    root = (config.get("strategy") or {}).get("alternate_entries")
    if not isinstance(root, dict) or not bool(root.get("enabled", False)):
        return None
    try:
        min_rs = int(root.get("min_regime_score", 2))
    except (TypeError, ValueError):
        min_rs = 2
    if regime_score is None or int(regime_score) < min_rs:
        return None

    order_raw = root.get("order")
    if isinstance(order_raw, (list, tuple)):
        order = [
            str(_enum_value_or_self(x)).strip().lower()
            for x in order_raw
            if str(_enum_value_or_self(x)).strip()
        ]
    else:
        order = ("breakout", "mean_reversion", "volatility")

    for kind in order:
        kind = str(_enum_value_or_self(kind)).strip().lower()
        if kind == "breakout":
            if breakout_intraday_allowlist is not None and not skip_breakout_intraday_prefilter:
                su = (symbol_upper or "").strip().upper()
                if not su or su not in breakout_intraday_allowlist:
                    continue
            if _breakout_ok(df, _subcfg(root, "breakout")):
                return AlternateEntryMatch("breakout")
        elif kind in ("mean_reversion", "meanrev", "mr"):
            if _mean_reversion_ok(df, _subcfg(root, "mean_reversion")):
                return AlternateEntryMatch("mean_reversion")
        elif kind in ("volatility", "vol"):
            if _volatility_ok(df, _subcfg(root, "volatility"), atr_pct=atr_pct):
                return AlternateEntryMatch("volatility")
        else:
            logger.debug("alternate_entries: unknown order key %r", kind)
    return None


def alternate_entry_signal_strength(config: Mapping[str, Any]) -> float:
    root = (config.get("strategy") or {}).get("alternate_entries")
    if not isinstance(root, dict):
        return 0.82
    try:
        v = float(root.get("signal_strength", 0.82))
    except (TypeError, ValueError):
        return 0.82
    return max(0.1, min(1.0, v))
