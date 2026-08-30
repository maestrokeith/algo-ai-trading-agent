"""Tests for alternate_entry_signals (live-loop non-trend entries)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.alternate_entry_signals import (
    alternate_entry_signal_strength,
    evaluate_alternate_entries,
)


def _cfg(**kwargs: object) -> dict:
    base = {
        "strategy": {
            "alternate_entries": {
                "enabled": True,
                "min_regime_score": 2,
                "order": ["breakout", "mean_reversion", "volatility"],
                "breakout": {"enabled": True, "lookback": 5},
                "mean_reversion": {"enabled": True, "rsi_period": 5, "rsi_max": 40, "ma_long": 20},
                "volatility": {
                    "enabled": True,
                    "atr_pct_min": 0.5,
                    "atr_pct_max": 50.0,
                    "daily_range_close_frac": 0.5,
                },
            }
        }
    }
    if kwargs:
        base["strategy"]["alternate_entries"].update(kwargs)
    return base


def test_evaluate_none_when_trend_ok() -> None:
    n = 80
    close = np.linspace(100.0, 150.0, n)
    df = pd.DataFrame(
        {
            "close": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "volume": np.full(n, 1e6),
        },
        index=pd.date_range("2020-01-01", periods=n, freq="D"),
    )
    assert evaluate_alternate_entries(df, _cfg(), trend_long_ok=True, regime_score=3, atr_pct=2.0) is None


def test_evaluate_none_when_disabled() -> None:
    n = 80
    close = np.linspace(100.0, 150.0, n)
    df = pd.DataFrame({"close": close, "high": close * 1.01, "low": close * 0.99}, index=range(n))
    assert (
        evaluate_alternate_entries(
            df, _cfg(enabled=False), trend_long_ok=False, regime_score=3, atr_pct=2.0
        )
        is None
    )


def test_breakout_match() -> None:
    n = 40
    # Flat highs then last close breaks above prior window max
    high = np.full(n, 100.0)
    high[-1] = 110.0
    close = np.full(n, 99.0)
    close[-1] = 109.0
    low = close - 1.0
    df = pd.DataFrame({"close": close, "high": high, "low": low}, index=range(n))
    m = evaluate_alternate_entries(
        df,
        _cfg(mean_reversion={"enabled": False}, volatility={"enabled": False}),
        trend_long_ok=False,
        regime_score=3,
        atr_pct=1.0,
    )
    assert m is not None
    assert m.kind == "breakout"


def test_breakout_blocked_when_empty_intraday_allowlist() -> None:
    """Core path: intraday prefilter empty set (e.g. regime below threshold) blocks breakout alternate."""
    n = 40
    high = np.full(n, 100.0)
    high[-1] = 110.0
    close = np.full(n, 99.0)
    close[-1] = 109.0
    low = close - 1.0
    df = pd.DataFrame({"close": close, "high": high, "low": low}, index=range(n))
    m = evaluate_alternate_entries(
        df,
        _cfg(mean_reversion={"enabled": False}, volatility={"enabled": False}),
        trend_long_ok=False,
        regime_score=3,
        atr_pct=1.0,
        symbol_upper="AAPL",
        breakout_intraday_allowlist=frozenset(),
        skip_breakout_intraday_prefilter=False,
    )
    assert m is None


def test_breakout_intraday_allowlist_bypass_for_dynamic_candidate() -> None:
    """Dynamic scanner names skip intraday allowlist; daily breakout pattern still applies."""
    n = 40
    high = np.full(n, 100.0)
    high[-1] = 110.0
    close = np.full(n, 99.0)
    close[-1] = 109.0
    low = close - 1.0
    df = pd.DataFrame({"close": close, "high": high, "low": low}, index=range(n))
    m = evaluate_alternate_entries(
        df,
        _cfg(mean_reversion={"enabled": False}, volatility={"enabled": False}),
        trend_long_ok=False,
        regime_score=3,
        atr_pct=1.0,
        symbol_upper="XYZ",
        breakout_intraday_allowlist=frozenset(),
        skip_breakout_intraday_prefilter=True,
    )
    assert m is not None
    assert m.kind == "breakout"


def test_breakout_when_symbol_in_intraday_allowlist() -> None:
    n = 40
    high = np.full(n, 100.0)
    high[-1] = 110.0
    close = np.full(n, 99.0)
    close[-1] = 109.0
    low = close - 1.0
    df = pd.DataFrame({"close": close, "high": high, "low": low}, index=range(n))
    m = evaluate_alternate_entries(
        df,
        _cfg(mean_reversion={"enabled": False}, volatility={"enabled": False}),
        trend_long_ok=False,
        regime_score=3,
        atr_pct=1.0,
        symbol_upper="AAPL",
        breakout_intraday_allowlist=frozenset({"AAPL"}),
        skip_breakout_intraday_prefilter=False,
    )
    assert m is not None
    assert m.kind == "breakout"


def test_mean_reversion_match() -> None:
    """RSI needs prior up/down moves (non-zero avg loss); keep last close above short MA."""
    n = 40
    close = 100.0 + 0.5 * np.sin(np.linspace(0, 6 * np.pi, n))
    close[-3:] = [close[-4], close[-4] - 0.35, close[-4] - 0.05]
    high = close * 1.001
    low = close * 0.999
    df = pd.DataFrame({"close": close, "high": high, "low": low}, index=range(n))
    m = evaluate_alternate_entries(
        df,
        _cfg(
            breakout={"enabled": False},
            volatility={"enabled": False},
            mean_reversion={
                "enabled": True,
                "rsi_period": 5,
                "rsi_max": 48.0,
                "ma_long": 5,
            },
        ),
        trend_long_ok=False,
        regime_score=3,
        atr_pct=2.0,
    )
    assert m is not None
    assert m.kind == "mean_reversion"


def test_volatility_match() -> None:
    n = 30
    close = np.full(n, 100.0)
    high = np.full(n, 105.0)
    low = np.full(n, 90.0)
    close[-1] = 103.0
    df = pd.DataFrame({"close": close, "high": high, "low": low}, index=range(n))
    m = evaluate_alternate_entries(
        df,
        _cfg(breakout={"enabled": False}, mean_reversion={"enabled": False}),
        trend_long_ok=False,
        regime_score=3,
        atr_pct=2.5,
    )
    assert m is not None
    assert m.kind == "volatility"


def test_regime_too_weak() -> None:
    n = 40
    close = np.linspace(50.0, 120.0, n)
    df = pd.DataFrame({"close": close, "high": close * 1.01, "low": close * 0.99}, index=range(n))
    assert (
        evaluate_alternate_entries(df, _cfg(), trend_long_ok=False, regime_score=1, atr_pct=2.0) is None
    )


def test_alternate_entry_signal_strength() -> None:
    assert alternate_entry_signal_strength({}) == pytest.approx(0.82)
