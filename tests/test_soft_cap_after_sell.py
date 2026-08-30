"""``recent_sell_within`` + soft-cap override after equity sell (``_LAST_EQUITY_SELL_UTC``)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.live import exits as exits_mod
from src.live.exits import LiveExitContext


@pytest.fixture
def _clear_last_sell() -> None:
    exits_mod._LAST_EQUITY_SELL_UTC.clear()
    yield
    exits_mod._LAST_EQUITY_SELL_UTC.clear()


def _ctx(now: datetime) -> LiveExitContext:
    return LiveExitContext(
        user_id="tuser",
        data_dir=MagicMock(),
        now=now,
        verbose=False,
        broker=MagicMock(),
        engine=MagicMock(),
        config={},
        account_equity=100_000.0,
        symbols=[],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )


def test_recent_sell_within_true_when_last_sell_recent(_clear_last_sell) -> None:
    t0 = datetime(2024, 6, 1, 15, 0, 0, tzinfo=timezone.utc)
    exits_mod._LAST_EQUITY_SELL_UTC["tuser"] = t0
    t1 = datetime(2024, 6, 1, 15, 2, 0, tzinfo=timezone.utc)  # +2 min
    assert _ctx(t1).recent_sell_within(5.0) is True


def test_recent_sell_within_false_when_expired(_clear_last_sell) -> None:
    t0 = datetime(2024, 6, 1, 15, 0, 0, tzinfo=timezone.utc)
    exits_mod._LAST_EQUITY_SELL_UTC["tuser"] = t0
    t1 = datetime(2024, 6, 1, 15, 6, 0, tzinfo=timezone.utc)  # +6 min
    assert _ctx(t1).recent_sell_within(5.0) is False


def test_recent_sell_within_zero_minutes(_clear_last_sell) -> None:
    assert _ctx(datetime.now(timezone.utc)).recent_sell_within(0.0) is False
