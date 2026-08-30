from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.live.exits import (
    _equity_unrealized_pnl_percent_points,
    do_not_sell_winners_early_blocks,
)
from src.strategy import TrendFollowingStrategy


def test_equity_unrealized_pnl_from_broker_fraction() -> None:
    p = _equity_unrealized_pnl_percent_points(
        {"unrealized_plpc": 0.034},
        entry_price=100.0,
        mid=100.0,
    )
    assert abs(p - 3.4) < 1e-6


def test_equity_unrealized_pnl_from_broker_percent_form() -> None:
    p = _equity_unrealized_pnl_percent_points(
        {"unrealized_plpc": 3.4},
        entry_price=100.0,
        mid=100.0,
    )
    assert abs(p - 3.4) < 1e-6


def test_equity_unrealized_pnl_fallback_entry_mid() -> None:
    p = _equity_unrealized_pnl_percent_points(
        {},
        entry_price=100.0,
        mid=105.0,
    )
    assert abs(p - 5.0) < 1e-6


@pytest.fixture
def strategy_dnw_on() -> TrendFollowingStrategy:
    return TrendFollowingStrategy(
        {
            "strategy": {
                "trend_following": {"ma_fast": 3, "ma_slow": 8},
                "trend_filter": {"require_above_both_ma": True},
                "retail": {
                    "ma_fast": 3,
                    "ma_slow": 8,
                    "time_bars_exit": 10,
                },
                "player_focus": "retail",
                "exits": {
                    "stop_loss_pct": 5.0,
                    "do_not_sell_winners_early": {"enabled": True, "min_pnl_pct": 2.0},
                },
            }
        }
    )


def test_do_not_sell_blocks_when_pnl_above_min_and_trend_strong(
    strategy_dnw_on: TrendFollowingStrategy,
) -> None:
    ctx = MagicMock()
    ctx.engine = MagicMock()
    ctx.engine.strategy = strategy_dnw_on
    ctx.broker = MagicMock()
    # long_trend_structure_still_strong -> True
    c = [100.0 + i * 0.5 for i in range(30)]
    df = pd.DataFrame(
        {
            "close": c,
            "high": [x * 1.01 for x in c],
            "low": [x * 0.99 for x in c],
        }
    )
    ctx.broker.get_bars.return_value = df
    assert do_not_sell_winners_early_blocks(ctx, "AAPL", 2.1) is True


def test_do_not_sell_not_blocks_when_pnl_at_or_below_min(
    strategy_dnw_on: TrendFollowingStrategy,
) -> None:
    ctx = MagicMock()
    ctx.engine = MagicMock()
    ctx.engine.strategy = strategy_dnw_on
    ctx.broker = MagicMock()
    c = [100.0 + i * 0.5 for i in range(30)]
    df = pd.DataFrame(
        {
            "close": c,
            "high": [x * 1.01 for x in c],
            "low": [x * 0.99 for x in c],
        }
    )
    ctx.broker.get_bars.return_value = df
    assert do_not_sell_winners_early_blocks(ctx, "AAPL", 2.0) is False
    assert do_not_sell_winners_early_blocks(ctx, "AAPL", 1.5) is False


def test_do_not_sell_sqqq_never_blocks_trend_path(
    strategy_dnw_on: TrendFollowingStrategy,
) -> None:
    ctx = MagicMock()
    ctx.engine = MagicMock()
    ctx.engine.strategy = strategy_dnw_on
    ctx.broker = MagicMock()
    # equity_long_trend_structure_still_strong short-circuits SQQQ to False
    assert do_not_sell_winners_early_blocks(ctx, "SQQQ", 10.0) is False
