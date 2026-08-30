"""
Market regime score: multi-factor sum used for sizing and entry policy.

regime_score = trend_score + volatility_score + breadth_score + macro_score

- trend_score (0–2): SPY above MA, QQQ above MA (each +1 when data available and rule passes).
- volatility_score (0–1): VIX proxy below threshold.
- breadth_score (0–1): HYG rising (close > shorter MA).
- macro_score (0–1): TLT falling (close < shorter MA).

Aggregate score 0–5. Condition: 4–5 bullish, 2–3 neutral, 0–1 defensive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class RegimeResult:
    score: int
    condition: str  # "bullish" | "neutral" | "defensive"
    size_multiplier: float
    details: dict[str, bool | str]  # per-rule flags, for logging
    factor_scores: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketRegimeDetection:
    """Directional and volatility regime classification."""

    direction: str
    volatility: str
    trend_pct: float
    realized_vol_pct: float
    vix: float | None

    @property
    def labels(self) -> tuple[str, str]:
        """Return direction and volatility labels."""
        return (self.direction, self.volatility)


def detect_market_regime(
    bars: dict[str, pd.DataFrame],
    *,
    market_symbol: str = "SPY",
    vix_symbol: str = "VIX",
    lookback: int = 20,
    bull_threshold_pct: float = 3.0,
    bear_threshold_pct: float = -3.0,
    high_vol_vix: float = 25.0,
    low_vol_vix: float = 15.0,
    high_realized_vol_pct: float = 2.0,
    low_realized_vol_pct: float = 0.75,
) -> MarketRegimeDetection:
    """Detect bull/bear/sideways and high/low volatility regimes."""
    market_df = bars.get(market_symbol)
    if market_df is None or market_df.empty or "close" not in market_df.columns:
        return MarketRegimeDetection("sideways", "low volatility", 0.0, 0.0, None)
    closes = market_df["close"].astype(float).dropna()
    if closes.empty:
        return MarketRegimeDetection("sideways", "low volatility", 0.0, 0.0, None)
    window = closes.tail(max(2, int(lookback)))
    first = float(window.iloc[0])
    last = float(window.iloc[-1])
    trend_pct = ((last - first) / first * 100.0) if first > 0 else 0.0
    returns = window.pct_change().dropna()
    realized_vol_pct = float(returns.std() * 100.0) if not returns.empty else 0.0

    if trend_pct >= bull_threshold_pct:
        direction = "bull"
    elif trend_pct <= bear_threshold_pct:
        direction = "bear"
    else:
        direction = "sideways"

    vix_df = bars.get(vix_symbol)
    vix: float | None = None
    if vix_df is not None and not vix_df.empty and "close" in vix_df.columns:
        vix = float(vix_df["close"].astype(float).dropna().iloc[-1])
    if (vix is not None and vix >= high_vol_vix) or realized_vol_pct >= high_realized_vol_pct:
        volatility = "high volatility"
    elif (vix is not None and vix <= low_vol_vix) or realized_vol_pct <= low_realized_vol_pct:
        volatility = "low volatility"
    else:
        volatility = "normal volatility"
    return MarketRegimeDetection(direction, volatility, trend_pct, realized_vol_pct, vix)


class MarketRegimeScorer:
    """
    trend_score: +1 SPY > MA(trend), +1 QQQ > MA(trend)
    volatility_score: +1 if VIX proxy < threshold
    breadth_score: +1 if HYG close > MA(short)
    macro_score: +1 if TLT close < MA(short)
    """

    def __init__(self, config: dict[str, Any]):
        mr = config.get("market_regime", {})
        self.enabled = bool(mr.get("enabled", True))
        symbols = mr.get("symbols", {})
        self.symbol_spy = (symbols.get("spy") or "SPY").strip().upper()
        self.symbol_qqq = (symbols.get("qqq") or "QQQ").strip().upper()
        self.symbol_vix = (symbols.get("vix") or "VIX").strip().upper()
        self.symbol_hyg = (symbols.get("hyg") or "HYG").strip().upper()
        self.symbol_tlt = (symbols.get("tlt") or "TLT").strip().upper()
        self.ma_period_trend = int(mr.get("ma_period_trend", 50))
        self.ma_period_rising_falling = int(mr.get("ma_period_rising_falling", 20))
        self.vix_threshold = float(mr.get("vix_threshold", 20.0))
        mult = mr.get("size_multipliers", {})
        self.mult_bullish = float(mult.get("bullish", 1.0))
        self.mult_neutral = float(mult.get("neutral", 0.8))
        self.mult_defensive = float(mult.get("defensive", 0.5))
        # Top-level ``regime:`` (YAML) overrides bullish/neutral sizing without nesting under ``market_regime``.
        reg_top = config.get("regime")
        if isinstance(reg_top, dict):
            raw_b = reg_top.get("bullish_size_mult")
            if raw_b is not None and str(raw_b).strip() != "":
                try:
                    self.mult_bullish = float(raw_b)
                except (TypeError, ValueError):
                    pass
            raw_n = reg_top.get("neutral_size_mult")
            if raw_n is not None and str(raw_n).strip() != "":
                try:
                    self.mult_neutral = float(raw_n)
                except (TypeError, ValueError):
                    pass
            raw_d = reg_top.get("defensive_size_mult")
            if raw_d is not None and str(raw_d).strip() != "":
                try:
                    self.mult_defensive = float(raw_d)
                except (TypeError, ValueError):
                    pass

    def required_symbols(self) -> list[str]:
        return [self.symbol_spy, self.symbol_qqq, self.symbol_vix, self.symbol_hyg, self.symbol_tlt]

    def compute(self, bars: dict[str, pd.DataFrame]) -> RegimeResult:
        """
        bars: map symbol -> DataFrame with columns open, high, low, close (and datetime index).
        Missing or empty DataFrames are treated as no contribution for that rule.
        """
        details: dict[str, bool | str] = {}

        def _close(sym: str):
            df = bars.get(sym)
            if df is None or df.empty or "close" not in df.columns:
                return None
            return float(df["close"].iloc[-1])

        def _ma(sym: str, period: int):
            df = bars.get(sym)
            if df is None or len(df) < period or "close" not in df.columns:
                return None
            return float(df["close"].rolling(period).mean().iloc[-1])

        trend_score = 0

        # SPY > MA(trend)
        spy_c, spy_ma = _close(self.symbol_spy), _ma(self.symbol_spy, self.ma_period_trend)
        if spy_c is not None and spy_ma is not None:
            ok = spy_c > spy_ma
            details[f"{self.symbol_spy}>MA{self.ma_period_trend}"] = ok
            if ok:
                trend_score += 1
        else:
            details[f"{self.symbol_spy}>MA{self.ma_period_trend}"] = "no data"

        # QQQ > MA(trend)
        qqq_c, qqq_ma = _close(self.symbol_qqq), _ma(self.symbol_qqq, self.ma_period_trend)
        if qqq_c is not None and qqq_ma is not None:
            ok = qqq_c > qqq_ma
            details[f"{self.symbol_qqq}>MA{self.ma_period_trend}"] = ok
            if ok:
                trend_score += 1
        else:
            details[f"{self.symbol_qqq}>MA{self.ma_period_trend}"] = "no data"

        volatility_score = 0
        vix_c = _close(self.symbol_vix)
        if vix_c is not None:
            ok = vix_c < self.vix_threshold
            details[f"{self.symbol_vix}<{self.vix_threshold}"] = ok
            if ok:
                volatility_score = 1
        else:
            details[f"{self.symbol_vix}<{self.vix_threshold}"] = "no data"

        breadth_score = 0
        hyg_c, hyg_ma = _close(self.symbol_hyg), _ma(self.symbol_hyg, self.ma_period_rising_falling)
        if hyg_c is not None and hyg_ma is not None:
            ok = hyg_c > hyg_ma
            details[f"{self.symbol_hyg} rising"] = ok
            if ok:
                breadth_score = 1
        else:
            details[f"{self.symbol_hyg} rising"] = "no data"

        macro_score = 0
        tlt_c, tlt_ma = _close(self.symbol_tlt), _ma(self.symbol_tlt, self.ma_period_rising_falling)
        if tlt_c is not None and tlt_ma is not None:
            ok = tlt_c < tlt_ma
            details[f"{self.symbol_tlt} falling"] = ok
            if ok:
                macro_score = 1
        else:
            details[f"{self.symbol_tlt} falling"] = "no data"

        factor_scores = {
            "trend_score": trend_score,
            "volatility_score": volatility_score,
            "breadth_score": breadth_score,
            "macro_score": macro_score,
        }
        score = trend_score + volatility_score + breadth_score + macro_score

        if score >= 4:
            condition = "bullish"
            size_multiplier = self.mult_bullish
        elif score >= 2:
            condition = "neutral"
            size_multiplier = self.mult_neutral
        else:
            condition = "defensive"
            size_multiplier = self.mult_defensive

        return RegimeResult(
            score=score,
            condition=condition,
            size_multiplier=size_multiplier,
            details=details,
            factor_scores=factor_scores,
        )
