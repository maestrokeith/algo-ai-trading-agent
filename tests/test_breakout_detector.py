"""Tests for breakout candidate detection and ranking."""

from __future__ import annotations

import pandas as pd

from src.strategies.breakout_detector import (
    breakout_score,
    breakout_signal,
    build_breakout_snapshot,
    find_breakouts,
    infer_symbol_sector,
    not_extended,
)


def _snapshot(**overrides: float | str) -> dict[str, float | str]:
    snapshot: dict[str, float | str] = {
        "symbol": "NVDA",
        "sector": "Technology",
        "volume_5m": 300_000.0,
        "avg_volume_5m": 100_000.0,
        "price": 101.0,
        "vwap": 100.0,
        "morning_high": 100.5,
        "ema9": 100.0,
        "ema21": 99.5,
        "rsi_5m": 60.0,
        "volume_spike_ratio": 3.0,
        "trend_strength": 0.8,
        "spread_pct": 0.2,
    }
    snapshot.update(overrides)
    return snapshot


def test_breakout_signal_happy_path() -> None:
    assert breakout_signal(_snapshot()) is True


def test_breakout_signal_rejects_failed_gate() -> None:
    assert breakout_signal(_snapshot(price=100.0)) is False
    assert breakout_signal(_snapshot(rsi_5m=55.0)) is False
    assert breakout_signal(_snapshot(rsi_5m=75.0)) is False
    assert breakout_signal(_snapshot(volume_5m=250_000.0)) is False


def test_not_extended_threshold_and_zero_ema_guard() -> None:
    assert not_extended(_snapshot(price=101.4, ema9=100.0)) is True
    assert not_extended(_snapshot(price=101.5, ema9=100.0)) is False
    assert not_extended(_snapshot(price=101.0, ema9=0.0)) is False


def test_breakout_score_uses_weighted_formula() -> None:
    snapshot = _snapshot(
        volume_spike_ratio=2.8,
        price=101.0,
        vwap=100.0,
        trend_strength=0.5,
        spread_pct=0.25,
    )
    expected = 0.4 * 2.8 + 0.3 * 0.01 + 0.2 * 0.5 - 0.1 * 0.25
    assert breakout_score(snapshot) == expected


def test_find_breakouts_filters_by_sector_signal_and_extension() -> None:
    universe = [
        _snapshot(symbol="NVDA"),
        _snapshot(symbol="JPM", sector="Financials"),
        _snapshot(symbol="AAPL", price=101.6, ema9=100.0),
        _snapshot(symbol="MSFT", rsi_5m=80.0),
    ]

    ranked = find_breakouts(universe, top_sectors=["Technology"])

    assert [candidate["symbol"] for candidate in ranked] == ["NVDA"]


def test_find_breakouts_ranks_descending_and_caps_results() -> None:
    universe = [
        _snapshot(symbol="NVDA", volume_spike_ratio=4.0, trend_strength=0.9, spread_pct=0.1),
        _snapshot(symbol="AAPL", volume_spike_ratio=3.5, trend_strength=0.8, spread_pct=0.2),
        _snapshot(symbol="MSFT", volume_spike_ratio=3.0, trend_strength=0.7, spread_pct=0.3),
        _snapshot(symbol="JPM", sector="Financials", volume_spike_ratio=10.0, trend_strength=1.0, spread_pct=0.0),
    ]

    ranked = find_breakouts(universe, top_sectors=["Technology", "Healthcare"])

    assert [candidate["symbol"] for candidate in ranked] == ["NVDA", "AAPL"]


def test_infer_symbol_sector_maps_known_symbols() -> None:
    assert infer_symbol_sector("NVDA") == "tech"
    assert infer_symbol_sector("JPM") == "finance"
    assert infer_symbol_sector("UNKNOWN") is None


def test_build_breakout_snapshot_from_intraday_bars() -> None:
    idx = pd.date_range("2026-04-30 09:30:00-04:00", periods=150, freq="1min")
    close = [100.0 + i * 0.04 for i in range(150)]
    bars = pd.DataFrame(
        {
            "open": [c - 0.05 for c in close],
            "high": [c + 0.10 for c in close],
            "low": [c - 0.10 for c in close],
            "close": close,
            "volume": [1_000.0] * 145 + [3_000.0, 3_200.0, 3_400.0, 3_600.0, 3_800.0],
        },
        index=idx,
    )

    snapshot = build_breakout_snapshot(
        symbol="NVDA",
        sector="tech",
        bars_1m=bars,
        spread_pct=0.2,
    )

    assert snapshot is not None
    assert snapshot["symbol"] == "NVDA"
    assert snapshot["sector"] == "tech"
    assert float(snapshot["avg_volume_5m"]) > 0
    assert float(snapshot["volume_spike_ratio"]) > 1.0
    assert float(snapshot["ema9"]) > float(snapshot["ema21"])
