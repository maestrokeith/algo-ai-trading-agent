"""Tests for bear ETF daily-bar gates (fresh MA cross)."""

from __future__ import annotations

import pandas as pd

from src.bear_etf_gates import daily_fresh_cross_below_ma


def _series_closes(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": values})


def test_insufficient_rows_returns_none() -> None:
    df = _series_closes([100.0, 101.0])
    assert daily_fresh_cross_below_ma(df, 50) is None


def test_fresh_cross_true() -> None:
    # MA(2): prev bar 104 >= MA(102,104)=103; curr 95 < MA(104,95)=99.5
    df = _series_closes([100.0, 102.0, 104.0, 95.0])
    assert daily_fresh_cross_below_ma(df, 2) is True


def test_stuck_below_ma_no_cross() -> None:
    df = _series_closes([50.0, 50.0, 40.0, 39.0])
    assert daily_fresh_cross_below_ma(df, 2) is False


def test_empty_returns_none() -> None:
    assert daily_fresh_cross_below_ma(pd.DataFrame(), 50) is None


def test_missing_close_column() -> None:
    assert daily_fresh_cross_below_ma(pd.DataFrame({"x": [1.0]}), 1) is None
