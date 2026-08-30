"""Tests for sector momentum ranking."""

from __future__ import annotations

import pandas as pd

from src.market.sector_strength import (
    SECTOR_ETFS,
    build_sector_snapshot,
    get_top_sectors,
    sector_strength,
)


def _sector_snapshot(**overrides: float) -> dict[str, float]:
    snapshot = {
        "price": 101.0,
        "vwap": 100.0,
        "momentum_30m": 0.5,
        "relative_volume": 1.2,
    }
    snapshot.update(overrides)
    return snapshot


def test_sector_etfs_mapping_contains_expected_keys() -> None:
    assert SECTOR_ETFS == {
        "tech": "XLK",
        "finance": "XLF",
        "energy": "XLE",
        "health": "XLV",
        "industrial": "XLI",
    }


def test_sector_strength_uses_all_components() -> None:
    snapshot = _sector_snapshot(price=102.0, vwap=100.0, momentum_30m=0.4, relative_volume=1.1)
    expected = 0.02 + 0.4 + 1.1
    assert sector_strength(snapshot) == expected


def test_get_top_sectors_returns_top_two_in_rank_order() -> None:
    sector_data = {
        "tech": _sector_snapshot(relative_volume=1.8, momentum_30m=0.7),
        "finance": _sector_snapshot(relative_volume=1.3, momentum_30m=0.4),
        "energy": _sector_snapshot(relative_volume=2.0, momentum_30m=0.9, price=103.0),
        "health": _sector_snapshot(relative_volume=1.0, momentum_30m=0.2),
    }

    assert get_top_sectors(sector_data) == ["energy", "tech"]


def test_get_top_sectors_handles_small_input() -> None:
    sector_data = {
        "industrial": _sector_snapshot(),
    }

    assert get_top_sectors(sector_data) == ["industrial"]


def test_build_sector_snapshot_from_intraday_bars() -> None:
    idx = pd.date_range("2026-04-30 09:30:00-04:00", periods=60, freq="1min")
    bars = pd.DataFrame(
        {
            "open": [100.0 + i * 0.01 for i in range(60)],
            "high": [100.2 + i * 0.01 for i in range(60)],
            "low": [99.8 + i * 0.01 for i in range(60)],
            "close": [100.0 + i * 0.02 for i in range(60)],
            "volume": [1_000.0 + i * 10.0 for i in range(60)],
        },
        index=idx,
    )

    snapshot = build_sector_snapshot(bars)

    assert snapshot is not None
    assert snapshot["price"] == 100.0 + 59 * 0.02
    assert snapshot["vwap"] > 0
    assert snapshot["momentum_30m"] > 0
    assert snapshot["relative_volume"] > 0
