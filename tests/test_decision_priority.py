"""Tests for src/decision_priority and LiveExitContext allocator buy veto."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.decision_priority import (
    DEFAULT_DECISION_PRIORITY,
    exit_reason_to_intent_kind,
    parse_decision_priority,
    rank_for_kind,
)
from src.live.exits import LiveExitContext
from src.strategy import ExitReason


def test_parse_decision_priority_defaults() -> None:
    assert parse_decision_priority(None) == DEFAULT_DECISION_PRIORITY
    assert parse_decision_priority({}) == DEFAULT_DECISION_PRIORITY


def test_parse_decision_priority_override() -> None:
    cfg = {"decision_priority": {"new_entry": 10, "exposure_trim": 2}}
    t = parse_decision_priority(cfg)
    assert t["new_entry"] == 10
    assert t["exposure_trim"] == 2
    assert t["stop_loss"] == DEFAULT_DECISION_PRIORITY["stop_loss"]


def test_parse_decision_priority_invalid_uses_default() -> None:
    cfg = {"decision_priority": {"stop_loss": "x"}}
    t = parse_decision_priority(cfg)
    assert t["stop_loss"] == DEFAULT_DECISION_PRIORITY["stop_loss"]


def test_rank_for_kind_unknown() -> None:
    assert rank_for_kind(DEFAULT_DECISION_PRIORITY, "nope") == 99


@pytest.mark.parametrize(
    "reason,kind",
    [
        (ExitReason.STOP_LOSS, "stop_loss"),
        (ExitReason.TRAILING_STOP, "stop_loss"),
        (ExitReason.KILL_SWITCH, "stop_loss"),
        (ExitReason.TAKE_PROFIT, "take_profit"),
        (ExitReason.PARTIAL_TAKE_PROFIT, "take_profit"),
        (ExitReason.RISK_CAP_REBALANCE, "exposure_trim"),
        (ExitReason.OVERWEIGHT_TRIM, "rebalance"),
        (ExitReason.TIME_BARS, "rebalance"),
    ],
)
def test_exit_reason_to_intent_kind(reason: ExitReason, kind: str) -> None:
    assert exit_reason_to_intent_kind(reason) == kind


def test_bulk_trim_register_blocks_buy_for_cooldown_window(tmp_path: Path) -> None:
    t0 = datetime(2026, 1, 5, 10, 0, 0, tzinfo=timezone.utc)
    eng = MagicMock()
    ctx = LiveExitContext(
        user_id="default",
        data_dir=tmp_path,
        now=t0,
        verbose=False,
        broker=None,
        engine=eng,
        config={
            "portfolio": {
                "rebalance_free_capital": {"bulk_trim": {"buy_cooldown_minutes": 30.0}}
            }
        },
        account_equity=100_000.0,
        symbols=["SPY"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    ctx.register_bulk_trim_sell("SPY", 30.0)
    b, m = ctx.bulk_trim_buy_cooldown_active("SPY")
    assert b is True
    assert m is not None
    ctx.now = t0 + timedelta(minutes=31)
    b2, _m2 = ctx.bulk_trim_buy_cooldown_active("SPY")
    assert b2 is False


def test_live_exit_context_blocks_allocator_buy_after_exposure_intent(tmp_path: Path) -> None:
    eng = MagicMock()
    ctx = LiveExitContext(
        user_id="default",
        data_dir=tmp_path,
        now=datetime(2026, 1, 5, 10, 0, 0),
        verbose=False,
        broker=None,
        engine=eng,
        config={"decision_priority": DEFAULT_DECISION_PRIORITY},
        account_equity=100_000.0,
        symbols=["WMT"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    ctx.note_decision_intent("WMT", "exposure_trim")
    blocked, msg = ctx.allocator_buy_blocked_by_priority("WMT")
    assert blocked is True
    assert msg is not None
    assert "rank" in msg


def test_live_exit_context_strongest_intent_wins(tmp_path: Path) -> None:
    eng = MagicMock()
    ctx = LiveExitContext(
        user_id="default",
        data_dir=tmp_path,
        now=datetime(2026, 1, 5, 10, 0, 0),
        verbose=False,
        broker=None,
        engine=eng,
        config={"decision_priority": DEFAULT_DECISION_PRIORITY},
        account_equity=100_000.0,
        symbols=["WMT"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    ctx.note_decision_intent("WMT", "rebalance")
    ctx.note_decision_intent("WMT", "exposure_trim")
    assert ctx._symbol_intent_best["WMT"] == rank_for_kind(DEFAULT_DECISION_PRIORITY, "exposure_trim")


def test_live_exit_context_no_block_when_trim_outranks_new_entry_in_config(tmp_path: Path) -> None:
    """If operator inverts priorities so new_entry is numerically first, trim does not veto."""
    eng = MagicMock()
    custom = dict(DEFAULT_DECISION_PRIORITY)
    custom["exposure_trim"] = 10
    custom["new_entry"] = 5
    ctx = LiveExitContext(
        user_id="default",
        data_dir=tmp_path,
        now=datetime(2026, 1, 5, 10, 0, 0),
        verbose=False,
        broker=None,
        engine=eng,
        config={"decision_priority": custom},
        account_equity=100_000.0,
        symbols=["WMT"],
        news_enabled=False,
        news_pipeline=None,
        news_rules=None,
    )
    ctx.note_decision_intent("WMT", "exposure_trim")
    blocked, _ = ctx.allocator_buy_blocked_by_priority("WMT")
    assert blocked is False
