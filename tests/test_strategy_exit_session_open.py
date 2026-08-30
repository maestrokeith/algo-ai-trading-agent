"""Defer stop-loss for first N minutes after regular session open (ET)."""

from __future__ import annotations

from src.strategy import ExitReason, TrendFollowingStrategy


def test_check_exit_defers_stop_loss_until_minutes_after_open() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "stop_loss_pct": 2.8,
                "avoid_stop_loss_first_minutes_after_open": 5,
                "time_bars_exit": 100,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    entry = 100.0
    current = 97.0  # -3% vs 2.8% stop
    assert s.check_exit("QQQ", entry, current, 1, minutes_since_session_open_et=3.0) is None
    sig = s.check_exit("QQQ", entry, current, 1, minutes_since_session_open_et=6.0)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS


def test_check_exit_stop_loss_when_defer_disabled() -> None:
    cfg = {
        "strategy": {
            "exits": {
                "stop_loss_pct": 2.8,
                "time_bars_exit": 100,
            }
        }
    }
    s = TrendFollowingStrategy(cfg)
    sig = s.check_exit("QQQ", 100.0, 97.0, 1, minutes_since_session_open_et=3.0)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS
