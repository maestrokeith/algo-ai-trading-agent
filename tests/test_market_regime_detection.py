from __future__ import annotations

import pandas as pd

from src.market_regime import detect_market_regime


def _bars(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": values})


def test_detect_market_regime_bull_low_volatility() -> None:
    result = detect_market_regime(
        {"SPY": _bars([100, 101, 102, 103, 104]), "VIX": _bars([12])},
        lookback=5,
    )

    assert result.direction == "bull"
    assert result.volatility == "low volatility"
    assert result.labels == ("bull", "low volatility")


def test_detect_market_regime_bear_high_volatility() -> None:
    result = detect_market_regime(
        {"SPY": _bars([100, 99, 95, 94, 92]), "VIX": _bars([30])},
        lookback=5,
    )

    assert result.direction == "bear"
    assert result.volatility == "high volatility"


def test_detect_market_regime_sideways_without_vix() -> None:
    result = detect_market_regime({"SPY": _bars([100, 100.5, 99.8, 100.1])}, lookback=4)

    assert result.direction == "sideways"
    assert result.vix is None


def test_detect_market_regime_missing_data_fallback() -> None:
    result = detect_market_regime({})

    assert result.direction == "sideways"
    assert result.volatility == "low volatility"
