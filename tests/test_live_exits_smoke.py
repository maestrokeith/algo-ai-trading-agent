"""Smoke import for live loop exit extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.live.exits import LiveExitContext, manage_option_position, manage_stock_position
from src.live.session_clock import minutes_since_regular_session_open_et


def test_minutes_since_open_at_premarket() -> None:
    from datetime import datetime

    import pytz

    et = pytz.timezone("America/New_York")
    dt = et.localize(datetime(2026, 4, 20, 9, 0, 0))
    assert minutes_since_regular_session_open_et(dt) == pytest.approx(0.0)


def test_live_exit_context_construct() -> None:
    from unittest.mock import MagicMock

    eng = MagicMock()
    ctx = LiveExitContext(
        user_id="u1",
        data_dir=Path("/tmp"),
        now=MagicMock(),
        verbose=False,
        broker=MagicMock(),
        engine=eng,
        config={},
        account_equity=100_000.0,
        symbols=["SPY"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    assert ctx.user_id == "u1"
    assert callable(manage_option_position)
    assert callable(manage_stock_position)


def test_live_exit_context_max_actions_per_symbol_per_cycle() -> None:
    from unittest.mock import MagicMock

    eng = MagicMock()
    cfg = {"execution": {"max_actions_per_symbol_per_cycle": 1}}
    ctx = LiveExitContext(
        user_id="u1",
        data_dir=Path("/tmp"),
        now=MagicMock(),
        verbose=False,
        broker=MagicMock(),
        engine=eng,
        config=cfg,
        account_equity=100_000.0,
        symbols=["SPY"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    assert ctx.max_actions_per_symbol_per_cycle() == 1
    assert ctx.exit_action_allowed("SPY") is True
    ctx.record_exit_action("SPY")
    assert ctx.exit_action_allowed("SPY") is False
    assert ctx.skip_exit_for_action_cap("SPY", "test") is True
    assert ctx.exit_action_allowed("QQQ") is True


def test_live_exit_context_unlimited_when_cap_zero() -> None:
    from unittest.mock import MagicMock

    ctx = LiveExitContext(
        user_id="u1",
        data_dir=Path("/tmp"),
        now=MagicMock(),
        verbose=False,
        broker=MagicMock(),
        engine=MagicMock(),
        config={"execution": {"max_actions_per_symbol_per_cycle": 0}},
        account_equity=100_000.0,
        symbols=["SPY"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    ctx.record_exit_action("SPY")
    ctx.record_exit_action("SPY")
    assert ctx.exit_action_allowed("SPY") is True
