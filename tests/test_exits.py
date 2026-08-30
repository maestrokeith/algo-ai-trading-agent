"""Tests for :mod:`src.exits`."""

from __future__ import annotations

import pytest

from src.exits import check_partial_exit


@pytest.mark.parametrize(
    "plpc,expected",
    [
        (0.05, 0.3),
        (0.041, 0.3),
        (0.03, 0.3),
        (0.021, 0.3),
        (0.02, 0.0),
        (0.0, 0.0),
        (-0.01, 0.0),
    ],
)
def test_check_partial_exit_thresholds(plpc: float, expected: float) -> None:
    assert check_partial_exit({"symbol": "SPY", "unrealized_plpc": plpc}) == expected


def test_check_partial_exit_intraday_key() -> None:
    assert check_partial_exit({"unrealized_intraday_plpc": 0.025}) == 0.3


def test_check_partial_exit_derived_from_pl_and_mv() -> None:
    assert check_partial_exit({"unrealized_pl": 300.0, "market_value": 6000.0}) == 0.3


def test_check_partial_exit_meaningful_profit_threshold() -> None:
    assert (
        check_partial_exit(
            {"unrealized_plpc": 0.014},
            partial_trim_trigger_pct=1.5,
            sell_fraction=0.2,
        )
        == 0.0
    )
    assert check_partial_exit(
        {"unrealized_plpc": 0.016},
        partial_trim_trigger_pct=1.5,
        sell_fraction=0.2,
    ) == pytest.approx(0.2)


def test_check_partial_exit_meaningful_profit_uses_trim_fraction_from_strategy() -> None:
    assert check_partial_exit(
        {"unrealized_plpc": 0.04},
        partial_trim_trigger_pct=1.5,
        sell_fraction=0.35,
    ) == pytest.approx(0.35)
