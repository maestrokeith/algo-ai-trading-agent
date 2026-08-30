"""Tests for ``execution.block_exits_if_no_reentry_capacity`` + LiveExitContext reentry gating."""
from __future__ import annotations

from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock

from src.execution import ExecutionManager
from src.live.exits import LiveExitContext, _reentry_block_allows_despite_flag
from src.strategy import ExitReason


def test_reentry_allows_protection_flags() -> None:
    assert _reentry_block_allows_despite_flag(ExitReason.STOP_LOSS) is True
    assert _reentry_block_allows_despite_flag(ExitReason.TRAILING_STOP) is True
    assert _reentry_block_allows_despite_flag(ExitReason.KILL_SWITCH) is True
    assert _reentry_block_allows_despite_flag(ExitReason.TAKE_PROFIT) is False


def test_reentry_block_when_bp_low(tmp_path: Path) -> None:
    broker = MagicMock()
    broker.get_buying_power = MagicMock(return_value=10.0)
    eng = MagicMock()
    eng.execution = ExecutionManager(
        {
            "execution": {"block_exits_if_no_reentry_capacity": True},
            "portfolio": {"min_cash_reserve_pct": 0.10},
            "entries": {"min_trade_size": 500},
        }
    )
    ctx = LiveExitContext(
        user_id="u",
        data_dir=tmp_path,
        now=datetime(2026, 1, 5, 10, 0, 0),
        verbose=False,
        broker=broker,
        engine=eng,
        config={
            "execution": {"block_exits_if_no_reentry_capacity": True},
            "portfolio": {"min_cash_reserve_pct": 0.10},
            "entries": {"min_trade_size": 500},
        },
        account_equity=20_000.0,
        symbols=[],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    b, _why = ctx.reentry_block_discretionary_sells()
    assert b is True


def test_reentry_block_off_when_config_false(tmp_path: Path) -> None:
    broker = MagicMock()
    broker.get_buying_power = MagicMock(return_value=5.0)
    eng = MagicMock()
    eng.execution = ExecutionManager(
        {
            "execution": {"block_exits_if_no_reentry_capacity": False},
            "entries": {"min_trade_size": 5000},
        }
    )
    ctx = LiveExitContext(
        user_id="u",
        data_dir=tmp_path,
        now=datetime(2026, 1, 5, 10, 0, 0),
        verbose=False,
        broker=broker,
        engine=eng,
        config={"execution": {"block_exits_if_no_reentry_capacity": False}, "entries": {"min_trade_size": 5000}},
        account_equity=20_000.0,
        symbols=[],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    assert ctx.reentry_block_discretionary_sells() == (False, None)
