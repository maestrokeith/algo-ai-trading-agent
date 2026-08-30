"""strong_trend_reconfirm_ok bypasses post-stop / post-profit cooldown; strong_momentum_reentry_ok bypasses profit re-entry price."""

from __future__ import annotations

import pandas as pd

from src.strategy import TrendFollowingStrategy


def _df_uptrend(*, n: int = 60) -> pd.DataFrame:
    """Rising closes so price stays above 10/50 MAs on last bar."""
    close = pd.Series(range(100, 100 + n), dtype=float)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": [1_000_000] * n,
        }
    )


def test_bypass_disabled_always_false() -> None:
    s = TrendFollowingStrategy({"strategy": {"exits": {"strong_trend_reconfirm_bypass_cooldown": False}}})
    df = _df_uptrend()
    assert not s.strong_trend_reconfirm_ok("QQQ", df, regime_score=5)


def test_bypass_requires_trend_structure() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "player_focus": "retail",
                "retail": {"ma_fast": 10, "ma_slow": 50},
                "trend_following": {"ma_fast": 10, "ma_slow": 50},
                "trend_filter": {"require_above_both_ma": True, "allow_above_fast_ma": False},
                "exits": {
                    "strong_trend_reconfirm_bypass_cooldown": True,
                    "strong_trend_reconfirm_min_regime_score": None,
                },
            }
        }
    )
    bad = pd.DataFrame(
        {
            "close": [100.0] * 60,
            "high": [101.0] * 60,
            "low": [99.0] * 60,
            "open": [100.0] * 60,
            "volume": [1e6] * 60,
        }
    )
    assert not s.strong_trend_reconfirm_ok("QQQ", bad, None)
    good = _df_uptrend()
    assert s.strong_trend_reconfirm_ok("QQQ", good, None)


def test_bypass_respects_min_regime_score() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "player_focus": "retail",
                "retail": {"ma_fast": 10, "ma_slow": 50},
                "trend_following": {"ma_fast": 10, "ma_slow": 50},
                "trend_filter": {"require_above_both_ma": True},
                "exits": {
                    "strong_trend_reconfirm_bypass_cooldown": True,
                    "strong_trend_reconfirm_min_regime_score": 4,
                },
            }
        }
    )
    df = _df_uptrend()
    assert not s.strong_trend_reconfirm_ok("QQQ", df, None)
    assert not s.strong_trend_reconfirm_ok("QQQ", df, 3)
    assert s.strong_trend_reconfirm_ok("QQQ", df, 4)


def test_momentum_reentry_bypass_disabled() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "player_focus": "retail",
                "retail": {"ma_fast": 10, "ma_slow": 50},
                "trend_following": {"ma_fast": 10, "ma_slow": 50},
                "trend_filter": {"require_above_both_ma": True},
                "exits": {"strong_momentum_bypass_profit_reentry_price": False},
            }
        }
    )
    df = _df_uptrend()
    assert not s.strong_momentum_reentry_ok("QQQ", df, 5)


def test_momentum_reentry_bypass_without_cooldown_bypass_flag() -> None:
    """Profit re-entry bypass uses MA/regime only; does not require strong_trend_reconfirm_bypass_cooldown."""
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "player_focus": "retail",
                "retail": {"ma_fast": 10, "ma_slow": 50},
                "trend_following": {"ma_fast": 10, "ma_slow": 50},
                "trend_filter": {"require_above_both_ma": True},
                "exits": {
                    "strong_trend_reconfirm_bypass_cooldown": False,
                    "strong_momentum_bypass_profit_reentry_price": True,
                    "strong_trend_reconfirm_min_regime_score": None,
                },
            }
        }
    )
    df = _df_uptrend()
    assert not s.strong_trend_reconfirm_ok("QQQ", df, 5)
    assert s.strong_momentum_reentry_ok("QQQ", df, 5)


def test_strong_momentum_structure_ok_ignores_profit_reentry_bypass_flag() -> None:
    """Portfolio allow_add_on_strong_momentum uses structure only; not gated on strong_momentum_bypass_profit_reentry_price."""
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "player_focus": "retail",
                "retail": {"ma_fast": 10, "ma_slow": 50},
                "trend_following": {"ma_fast": 10, "ma_slow": 50},
                "trend_filter": {"require_above_both_ma": True},
                "exits": {
                    "strong_momentum_bypass_profit_reentry_price": False,
                    "strong_trend_reconfirm_min_regime_score": None,
                },
            }
        }
    )
    df = _df_uptrend()
    assert not s.strong_momentum_reentry_ok("QQQ", df, 5)
    assert s.strong_momentum_structure_ok("QQQ", df, 5)


def test_strong_momentum_structure_ok_respects_min_regime_score() -> None:
    s = TrendFollowingStrategy(
        {
            "strategy": {
                "player_focus": "retail",
                "retail": {"ma_fast": 10, "ma_slow": 50},
                "trend_following": {"ma_fast": 10, "ma_slow": 50},
                "trend_filter": {"require_above_both_ma": True},
                "exits": {
                    "strong_trend_reconfirm_min_regime_score": 4,
                },
            }
        }
    )
    df = _df_uptrend()
    assert not s.strong_momentum_structure_ok("QQQ", df, None)
    assert not s.strong_momentum_structure_ok("QQQ", df, 3)
    assert s.strong_momentum_structure_ok("QQQ", df, 4)
