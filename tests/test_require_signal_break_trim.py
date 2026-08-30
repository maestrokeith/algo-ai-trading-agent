from __future__ import annotations

import pandas as pd

from src.strategy import TrendFollowingStrategy


def _cfg() -> dict:
    return {
        "strategy": {
            "trend_following": {"ma_fast": 3, "ma_slow": 8},
            "trend_filter": {

                "require_above_both_ma": True,
            },
            "retail": {
                "ma_fast": 3,
                "ma_slow": 8,
                "time_bars_exit": 10,
            },
            "player_focus": "retail",
            "exits": {
                "require_signal_break_for_trim": True,
                "stop_loss_pct": 5.0,
            },
        }
    }


def _uptrend_closes(n: int = 30) -> list[float]:
    return [100.0 + i * 0.5 for i in range(n)]


def test_long_trend_structure_still_strong_true_in_uptrend() -> None:
    s = TrendFollowingStrategy(_cfg())
    c = _uptrend_closes(30)
    df = pd.DataFrame(
        {
            "close": c,
            "high": [x * 1.01 for x in c],
            "low": [x * 0.99 for x in c],
        }
    )
    assert s.long_trend_structure_still_strong("AAPL", df) is True


def test_long_trend_structure_still_strong_false_below_mas() -> None:
    s = TrendFollowingStrategy(_cfg())
    c = [150.0 - i * 2.0 for i in range(30)]
    df = pd.DataFrame(
        {
            "close": c,
            "high": [x * 1.01 for x in c],
            "low": [x * 0.99 for x in c],
        }
    )
    assert s.long_trend_structure_still_strong("AAPL", df) is False


def test_sqqq_never_strong_for_trim_gate() -> None:
    s = TrendFollowingStrategy(_cfg())
    c = _uptrend_closes(30)
    df = pd.DataFrame(
        {
            "close": c,
            "high": [x * 1.01 for x in c],
            "low": [x * 0.99 for x in c],
        }
    )
    assert s.long_trend_structure_still_strong("SQQQ", df) is False


def test_trend_following_default_require_signal_off() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "trend_following": {},
                "exits": {
                    "stop_loss_pct": 2.0,
                },
            }
        }
    )
    assert s.require_signal_break_for_trim is False


def test_do_not_sell_winners_early_config_defaults() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "trend_following": {},
                "exits": {
                    "stop_loss_pct": 2.0,
                },
            }
        }
    )
    assert s.do_not_sell_winners_early_enabled is False
    assert s.do_not_sell_winners_early_min_pnl_pct == 2.0


def test_do_not_sell_winners_early_config_parsed() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "trend_following": {},
                "exits": {
                    "stop_loss_pct": 2.0,
                    "do_not_sell_winners_early": {
                        "enabled": True,
                        "min_pnl_pct": 3.5,
                    },
                },
            }
        }
    )
    assert s.do_not_sell_winners_early_enabled is True
    assert s.do_not_sell_winners_early_min_pnl_pct == 3.5
