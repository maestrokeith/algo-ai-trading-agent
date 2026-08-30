"""Tests for score-2 SQQQ fresh-cross skip and optional OR filters."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytz

from src.sqqq_entry_gates import (
    score_2_skip_fresh_cross_below_ma,
    score_2_sqqq_optional_filters_pass,
    _qqq_ma20_below_ma50,
)

ET = pytz.timezone("America/New_York")


def test_skip_fresh_cross_only_at_score_2() -> None:
    cfg = {"market_regime": {"entry_policy": {"score_2_sqqq_skip_fresh_cross": True}}}
    assert score_2_skip_fresh_cross_below_ma(cfg, 2) is True
    assert score_2_skip_fresh_cross_below_ma(cfg, 3) is False


def test_skip_fresh_cross_can_disable() -> None:
    cfg = {"market_regime": {"entry_policy": {"score_2_sqqq_skip_fresh_cross": False}}}
    assert score_2_skip_fresh_cross_below_ma(cfg, 2) is False


def test_optional_filters_off_always_pass() -> None:
    cfg = {"market_regime": {"entry_policy": {}}}
    ok, msg = score_2_sqqq_optional_filters_pass(cfg, MagicMock(), now_et=ET.localize(datetime(2026, 3, 31, 11, 0)), qqq_last=400.0)
    assert ok is True
    assert msg == ""


def test_ma20_below_ma50_mock() -> None:
    # declining structure: last closes make MA20 < MA50
    closes = [100.0 - i * 0.15 for i in range(60)]
    df = pd.DataFrame({
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [1e6] * 60,
    })
    br = MagicMock()
    br.get_bars = MagicMock(return_value=df)
    assert _qqq_ma20_below_ma50(br, "QQQ") is True


def test_optional_filters_ma_only_pass() -> None:
    closes = [100.0 - i * 0.15 for i in range(60)]
    df = pd.DataFrame({
        "open": closes,
        "high": closes,
        "low": closes,
        "close": closes,
        "volume": [1e6] * 60,
    })
    br = MagicMock()
    br.get_bars = MagicMock(return_value=df)
    cfg = {"market_regime": {"entry_policy": {"score_2_sqqq_filter_ma20_below_ma50": True}}}
    now = ET.localize(datetime(2026, 3, 31, 11, 0))
    ok, msg = score_2_sqqq_optional_filters_pass(cfg, br, now_et=now, qqq_last=95.0)
    assert ok is True
    assert "MA20" in msg


def test_optional_filters_or_red_when_ma_fails() -> None:
    """Enabled: MA (fails) + red day (passes) → OK."""
    rising = [50.0 + i * 0.2 for i in range(60)]
    df50 = pd.DataFrame({
        "open": rising,
        "high": rising,
        "low": rising,
        "close": rising,
        "volume": [1e6] * 60,
    })
    df1d = pd.DataFrame({
        "open": [400.0, 400.0],
        "high": [410.0, 401.0],
        "low": [395.0, 390.0],
        "close": [405.0, 395.0],
        "volume": [1e9, 1e8],
    })

    def _bars(sym, timeframe="1Day", **kwargs):
        if timeframe == "1Day" and int(kwargs.get("limit", 300)) <= 2:
            return df1d
        return df50

    br = MagicMock()
    br.get_bars = MagicMock(side_effect=_bars)
    cfg = {
        "market_regime": {
            "entry_policy": {
                "score_2_sqqq_filter_ma20_below_ma50": True,
                "score_2_sqqq_filter_red_day": True,
            }
        }
    }
    now = ET.localize(datetime(2026, 3, 31, 11, 0))
    ok, msg = score_2_sqqq_optional_filters_pass(cfg, br, now_et=now, qqq_last=395.0)
    assert ok is True
    assert "red" in msg.lower()
