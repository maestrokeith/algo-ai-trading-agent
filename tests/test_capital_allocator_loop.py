"""Tests for :mod:`src.capital_allocator_loop` (``place_order``)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.capital_allocator_loop import (
    _ALLOCATOR_EMPTY_ACTION_CYCLES,
    _allocator_weak_catalyst_late_entry_decision,
    _allocator_live_weak_catalyst_exception_experiment_decision,
    _apply_high_conviction_rotation_relaxation,
    _dynamic_min_price_from_config,
    _log_allocation_gap_report,
    _normalized_correlation_groups,
    build_dynamic_aggressive_scalp_candidates,
    clean_notional,
    allocator_position_score,
    build_allocator_candidates,
    build_allocator_portfolio,
    compute_score,
    empty_alloc_equal_split_buys,
    empty_alloc_fixed_size_buys,
    ensure_minimum_viable_allocator_buy_notional,
    execute_capital_allocator_pass,
    place_order,
    rank_allocator_candidates,
    select_top_candidates_with_group_cap,
    take_top_deploy_candidates,
    trend_long_strength_uses_equity_allocator,
)
from src.dynamic_universe import _dynamic_scan_settings
from src.execution import ExecutionManager
from src.position_state_machine import record_sell_after_exit
from src.position_tracker import save as save_tracked
from src.trade_attribution import record_exit, record_order_event
from src.trading_control import ENTRY_BLOCKED_MODE_ENTRIES_DISABLED, EntryBlocked


class _Quote:
    def __init__(self, *, mid: float = 100.0, spread_pct: float = 0.1) -> None:
        self.mid = mid
        self.bid = mid * 0.999
        self.ask = mid * 1.001
        self.spread_pct = spread_pct
        self.skip_spread_check = False

    def is_stale(self, _max_age: float) -> bool:
        return False

    def reference_mid(self, fallback: float) -> float:
        return float(self.mid or fallback)


class _TerminalRecorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record_entry_terminal_outcome(self, **kwargs) -> None:
        self.rows.append(dict(kwargs))


def test_place_order_buy_delegates_to_execution_and_broker() -> None:
    broker = MagicMock()
    broker.submit_order = MagicMock(return_value=MagicMock(id="o1"))
    engine = MagicMock()
    req = MagicMock()
    engine.execution.build_order_from_dict = MagicMock(return_value=req)

    out = place_order(
        broker,
        engine,
        {"action": "buy", "symbol": "spy", "notional": 1000.0},
        mid_price=500.0,
        spread_pct=0.05,
        ignore_spread_gate=False,
        bid=499.0,
        ask=501.0,
    )

    assert out is not None
    engine.execution.build_order_from_dict.assert_called_once()
    spec = engine.execution.build_order_from_dict.call_args[0][0]
    assert spec["route"] is None
    assert spec["source"] is None
    _kw = engine.execution.build_order_from_dict.call_args.kwargs
    assert _kw["mid_price"] == pytest.approx(500.0)
    broker.submit_order.assert_called_once_with(req)


def test_place_order_bounded_live_bypasses_normal_minimum_after_resizing(tmp_path: Path) -> None:
    broker = MagicMock()
    broker.is_asset_fractionable.return_value = True
    broker.submit_order = MagicMock(return_value=MagicMock(id="o1"))
    engine = MagicMock()
    engine.execution = ExecutionManager(
        {"execution": {"min_trade_dollars": 1200.0, "max_spread_pct": 5.0, "prefer_limit_orders": False}}
    )
    cfg = {
        "trading_control": {
            "mode": "live",
            "live_pilot": {
                "enabled": True,
                "allowed_strategies": ["trend_long"],
                "max_notional_per_trade": 100.0,
                "max_total_deployed_notional": 100.0,
            },
        }
    }

    out = place_order(
        broker,
        engine,
        {"action": "buy", "symbol": "XLE", "notional": 1200.0, "route": "trend_long"},
        mid_price=25.0,
        spread_pct=0.05,
        config=cfg,
        data_dir=tmp_path,
        user_id="u",
    )

    assert out is not None
    req = broker.submit_order.call_args.args[0]
    assert req.notional == 100.0
    assert req.quantity == 0.0
    assert req.route == "trend_long"
    assert req.source == "trend_long"
    assert req.strategy == "trend_long"


def test_place_order_shadow_sizing_still_uses_normal_minimum(tmp_path: Path) -> None:
    broker = MagicMock()
    engine = MagicMock()
    engine.execution = ExecutionManager(
        {"execution": {"min_trade_dollars": 1200.0, "max_spread_pct": 5.0, "prefer_limit_orders": False}}
    )
    cfg = {
        "trading_control": {
            "mode": "shadow",
            "live_pilot": {
                "enabled": True,
                "allowed_strategies": ["trend_long"],
                "max_notional_per_trade": 100.0,
                "max_total_deployed_notional": 100.0,
            },
        }
    }

    out = place_order(
        broker,
        engine,
        {"action": "buy", "symbol": "XLE", "notional": 100.0, "route": "trend_long"},
        mid_price=25.0,
        spread_pct=0.05,
        config=cfg,
        data_dir=tmp_path,
        user_id="u",
    )

    assert out is None
    assert engine.execution.last_order_build_reject_reason == "notional $100.00 below min_trade_dollars $1200.00"
    broker.submit_order.assert_not_called()


def test_place_order_bounded_live_blocks_invalid_final_size_without_broker_call(tmp_path: Path) -> None:
    broker = MagicMock()
    engine = MagicMock()
    engine.execution = ExecutionManager(
        {"execution": {"min_trade_dollars": 1200.0, "max_spread_pct": 5.0, "prefer_limit_orders": False}}
    )
    cfg = {
        "trading_control": {
            "mode": "live",
            "live_pilot": {
                "enabled": True,
                "allowed_strategies": ["trend_long"],
                "max_notional_per_trade": 100.0,
                "max_total_deployed_notional": 100.0,
            },
        }
    }

    out = place_order(
        broker,
        engine,
        {"action": "buy", "symbol": "XLE", "notional": 1200.0, "route": "trend_long"},
        mid_price=0.0,
        spread_pct=0.05,
        config=cfg,
        data_dir=tmp_path,
        user_id="u",
    )

    assert out is None
    assert engine.execution.last_order_build_reject_reason == "invalid_reference_price"
    broker.submit_order.assert_not_called()


def test_place_order_full_exit_sells_exact_fractional_qty() -> None:
    broker = MagicMock()
    broker.available_position_qty.return_value = (0.771667788, 0.0, 0.771667788)
    broker.close_position.return_value = MagicMock(id="close-pltr")
    engine = MagicMock()
    engine.execution.build_order_from_dict = MagicMock()

    out = place_order(
        broker,
        engine,
        {
            "action": "sell",
            "symbol": "PLTR",
            "notional": 86.34,
            "route": "stop_loss",
        },
        mid_price=111.89,
        spread_pct=0.05,
    )

    assert out.id == "close-pltr"
    broker.close_position.assert_called_once_with("PLTR")
    broker.submit_notional_market_day.assert_not_called()
    engine.execution.build_order_from_dict.assert_not_called()


def test_place_order_partial_trim_does_not_trigger_full_close() -> None:
    broker = MagicMock()
    broker.submit_order = MagicMock(return_value=MagicMock(id="trim-pltr"))
    engine = MagicMock()
    req = MagicMock()
    engine.execution.build_order_from_dict = MagicMock(return_value=req)

    out = place_order(
        broker,
        engine,
        {
            "action": "sell",
            "symbol": "PLTR",
            "notional": 250.00,
            "route": "rebalance_trim",
        },
        mid_price=111.89,
        spread_pct=0.05,
    )

    assert out.id == "trim-pltr"
    broker.close_position.assert_not_called()
    broker.submit_order.assert_called_once_with(req)
    engine.execution.build_order_from_dict.assert_called_once()


def test_place_order_preserves_route_metadata_and_submits_with_real_execution() -> None:
    broker = MagicMock()
    broker.submit_order = MagicMock(return_value=MagicMock(id="o-core"))
    engine = MagicMock()
    engine.execution = ExecutionManager({"execution": {"max_spread_pct": 5.0, "prefer_limit_orders": False}})

    out = place_order(
        broker,
        engine,
        {
            "action": "buy",
            "symbol": "SMH",
            "notional": 1200.0,
            "route": "core_rebuild",
            "source": "core_rebuild",
            "core_rebuild": True,
        },
        mid_price=250.0,
        spread_pct=0.05,
        ignore_spread_gate=False,
        bid=249.9,
        ask=250.1,
    )

    assert out is not None
    broker.submit_order.assert_called_once()
    req = broker.submit_order.call_args.args[0]
    assert req.symbol == "SMH"
    assert req.notional == pytest.approx(1200.0)


def test_place_order_sell_side() -> None:
    broker = MagicMock()
    engine = MagicMock()
    engine.execution.build_order_from_dict = MagicMock(return_value=MagicMock())
    place_order(
        broker,
        engine,
        {"action": "sell", "symbol": "QQQ", "notional": 250.0},
        mid_price=400.0,
        spread_pct=0.02,
    )
    spec = engine.execution.build_order_from_dict.call_args[0][0]
    assert spec["side"] == "sell"
    assert spec["notional"] == pytest.approx(250.0)


def test_trend_long_strength_uses_equity_allocator_options_routing() -> None:
    """``strength > min`` and options on → not equity batch; at/below or options off → equity batch."""
    assert not trend_long_strength_uses_equity_allocator(
        strength_eff=0.90,
        strong_signal_strength_min=0.85,
        options_enabled=True,
        options_allow_new_entries=True,
    )
    assert trend_long_strength_uses_equity_allocator(
        strength_eff=0.85,
        strong_signal_strength_min=0.85,
        options_enabled=True,
        options_allow_new_entries=True,
    )
    assert trend_long_strength_uses_equity_allocator(
        strength_eff=0.9,
        strong_signal_strength_min=0.85,
        options_enabled=True,
        options_allow_new_entries=False,
    )
    assert trend_long_strength_uses_equity_allocator(
        strength_eff=0.9,
        strong_signal_strength_min=0.85,
        options_enabled=False,
        options_allow_new_entries=True,
    )


def test_clean_notional_drops_invalid_and_rounds_down() -> None:
    assert clean_notional(100.556) == pytest.approx(100.55)
    assert clean_notional("250.1") == pytest.approx(250.1)
    assert clean_notional(0.5) == 0.0
    assert clean_notional(1.0) == pytest.approx(1.0)
    assert clean_notional(100.556, min_notional=0.0) == pytest.approx(100.55)
    assert clean_notional(-1) == 0.0
    assert clean_notional(float("nan")) == 0.0
    assert clean_notional(float("inf")) == 0.0
    assert clean_notional("x") == 0.0


def test_ensure_minimum_viable_allocator_buy_notional_bumps_to_one_share() -> None:
    assert ensure_minimum_viable_allocator_buy_notional(100.0, ref_price=250.13) == pytest.approx(250.13)
    assert ensure_minimum_viable_allocator_buy_notional(300.0, ref_price=250.13) == pytest.approx(300.0)
    assert ensure_minimum_viable_allocator_buy_notional(0.01, ref_price=999.99) == pytest.approx(999.99)


def test_place_order_returns_none_when_invalid() -> None:
    broker = MagicMock()
    engine = MagicMock()
    engine.execution.build_order_from_dict = MagicMock(return_value=None)
    assert (
        place_order(
            broker,
            engine,
            {"action": "hold", "symbol": "X", "notional": 1.0},
            mid_price=1.0,
            spread_pct=0.01,
        )
        is None
    )
    broker.submit_order.assert_not_called()


def test_place_order_returns_none_when_notional_nonpositive_after_clean() -> None:
    broker = MagicMock()
    engine = MagicMock()
    assert (
        place_order(
            broker,
            engine,
            {"action": "buy", "symbol": "SPY", "notional": -5.0},
            mid_price=1.0,
            spread_pct=0.01,
        )
        is None
    )
    engine.execution.build_order_from_dict.assert_not_called()


def test_compute_score_uses_tracker() -> None:
    tracked = {"SPY": {"signal_strength": 1.25}}
    assert compute_score("SPY", tracked) == pytest.approx(1.25)
    assert allocator_position_score("SPY", tracked) == pytest.approx(1.25)


def test_build_allocator_portfolio_from_positions() -> None:
    positions = [
        {"symbol": "MSFT", "qty": 2, "market_value": 800.0},
    ]
    tracked = {"MSFT": {"signal_strength": 0.9}}
    elig = {"MSFT", "AAPL"}
    rows = build_allocator_portfolio(positions, tracked, elig)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["value"] == pytest.approx(800.0)
    assert rows[0]["score"] == pytest.approx(0.9)


def test_build_allocator_portfolio_accepts_position_objects() -> None:
    class Pos:
        def __init__(self) -> None:
            self.symbol = "NVDA"
            self.market_value = 500.0

    tracked = {"NVDA": {"signal_strength": 1.1}}
    rows = build_allocator_portfolio([Pos()], tracked, {"NVDA"})
    assert len(rows) == 1
    assert rows[0]["symbol"] == "NVDA"
    assert rows[0]["value"] == pytest.approx(500.0)
    assert rows[0]["score"] == pytest.approx(1.1)


def test_build_allocator_candidates_all_signals_no_final_filter() -> None:
    rows = [
        {"sym_u": "SPY", "strength_eff": 0.9, "final": True},
        {"sym_u": "QQQ", "strength_eff": 0.8, "final": False},
        {"sym_u": "IWM", "strength_eff": 0.7},
    ]
    c = build_allocator_candidates(rows)
    syms = {x["symbol"] for x in c}
    assert syms == {"SPY", "QQQ", "IWM"}
    assert next(x["score"] for x in c if x["symbol"] == "SPY") == pytest.approx(0.9)
    assert next(x["score"] for x in c if x["symbol"] == "QQQ") == pytest.approx(0.8)


def test_build_allocator_candidates_prefers_score_key_when_present() -> None:
    c = build_allocator_candidates([{"symbol": "X", "score": 1.1, "strength_eff": 9.0}])
    assert len(c) == 1
    assert c[0]["symbol"] == "X"
    assert c[0]["score"] == pytest.approx(1.1)


def test_build_allocator_candidates_prefers_composite_over_strength_for_rank() -> None:
    c = build_allocator_candidates(
        [{"sym_u": "Z", "composite_score": 3.2, "strength_eff": 0.5}]
    )
    assert len(c) == 1
    assert c[0]["score"] == pytest.approx(3.2)
    assert c[0]["strength_eff"] == pytest.approx(0.5)


def test_build_allocator_candidates_boosts_dynamic_candidate_score() -> None:
    c = build_allocator_candidates(
        [
            {"sym_u": "CORE", "composite_score": 1.2},
            {"sym_u": "DYN", "composite_score": 1.0, "dynamic_candidate": True},
        ]
    )
    dyn = next(x for x in c if x["symbol"] == "DYN")
    assert dyn["score"] == pytest.approx(1.35)
    assert dyn["dynamic_candidate"] is True
    assert dyn["score_multiplier"] == pytest.approx(1.35)
    assert [x["symbol"] for x in rank_allocator_candidates(c)] == ["DYN", "CORE"]


def test_build_allocator_candidates_preserves_filter_diagnostic_metadata() -> None:
    c = build_allocator_candidates(
        [
            {
                "sym_u": "CMND",
                "score": 4.2,
                "dynamic_candidate": True,
                "news_score": 0,
                "event_score": 0,
                "catalyst_score": 0,
                "article_count": 0,
                "age_minutes": 17,
                "catalyst_age_minutes": 17,
                "catalyst_type": "momentum",
                "catalyst_headline": "CMND scanner momentum",
                "premarket_injected": False,
                "catalyst_fastlane_active": False,
                "route": "dynamic_scan",
                "dynamic_score": 42.0,
                "scanner_score": 42.0,
                "signal_score": 42.0,
                "gain_pct": 4.5,
                "day_gain_pct": 4.5,
                "relative_volume": 1.25,
                "spread_pct": 0.2,
                "is_dynamic": True,
                "candidate_notional_requested": 1_312.50,
                "requested_notional": 1_500.00,
            }
        ]
    )

    assert c == [
        {
            "symbol": "CMND",
            "score": pytest.approx(4.2 * 1.35),
            "dynamic_candidate": True,
            "score_multiplier": pytest.approx(1.35),
            "news_score": pytest.approx(0),
            "event_score": pytest.approx(0),
            "catalyst_score": pytest.approx(0),
            "article_count": pytest.approx(0),
            "age_minutes": pytest.approx(17),
            "catalyst_age_minutes": pytest.approx(17),
            "catalyst_type": "momentum",
            "catalyst_headline": "CMND scanner momentum",
            "premarket_injected": False,
            "catalyst_fastlane_active": False,
            "route": "dynamic_scan",
            "dynamic_score": pytest.approx(42.0),
            "scanner_score": pytest.approx(42.0),
            "signal_score": pytest.approx(42.0),
            "gain_pct": pytest.approx(4.5),
            "day_gain_pct": pytest.approx(4.5),
            "relative_volume": pytest.approx(1.25),
            "spread_pct": pytest.approx(0.2),
            "is_dynamic": True,
            "candidate_notional_requested": pytest.approx(1_312.50),
            "requested_notional": pytest.approx(1_500.00),
        }
    ]


def _weak_dynamic_candidate(**overrides) -> dict:
    row = {
        "symbol": "AIIO",
        "route": "dynamic_momentum_override",
        "source": "dynamic_universe",
        "dynamic_candidate": True,
        "dynamic_symbol": True,
        "entry_eval_final": True,
        "decision_allowed": True,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
        "relative_volume": 1.5,
        "scanner_relative_volume": 1.5,
        "gain_pct": 10.0,
        "day_gain_pct": 10.0,
        "price_above_vwap": True,
        "new_intraday_high": True,
        "spread_pct": 0.2,
        "distance_from_vwap_pct": 2.0,
    }
    row.update(overrides)
    return row


def test_weak_catalyst_dynamic_at_10pct_allows_when_aligned() -> None:
    decision = _allocator_weak_catalyst_late_entry_decision(
        _weak_dynamic_candidate(gain_pct=10.0, day_gain_pct=10.0),
        config={"broker": {"paper": False}, "dynamic_universe": {}},
        user_id="live_bot",
        spread_pct=0.2,
        spread_cap_pct=2.5,
    )

    assert decision["action"] == "allow"
    assert decision["reason"] == "within_early_entry_window"


def test_weak_catalyst_dynamic_at_13pct_reduces_live_size() -> None:
    decision = _allocator_weak_catalyst_late_entry_decision(
        _weak_dynamic_candidate(gain_pct=13.0, day_gain_pct=13.0),
        config={
            "broker": {"paper": False},
            "dynamic_universe": {"weak_catalyst_late_entry_reduction": 0.5},
        },
        user_id="live_bot",
        spread_pct=0.2,
        spread_cap_pct=2.5,
    )

    assert decision["action"] == "reduce"
    assert decision["factor"] == pytest.approx(0.5)


def test_weak_catalyst_dynamic_at_20pct_blocks_without_exceptional_confirmation() -> None:
    decision = _allocator_weak_catalyst_late_entry_decision(
        _weak_dynamic_candidate(
            gain_pct=20.0,
            day_gain_pct=20.0,
            relative_volume=2.5,
            scanner_relative_volume=2.5,
            new_intraday_high=False,
        ),
        config={"broker": {"paper": False}, "dynamic_universe": {}},
        user_id="live_bot",
        spread_pct=0.2,
        spread_cap_pct=2.5,
    )

    assert decision["action"] == "block"
    assert decision["reason"] == "late_chase_protection"


def test_weak_catalyst_dynamic_at_20pct_allows_exceptional_confirmation() -> None:
    decision = _allocator_weak_catalyst_late_entry_decision(
        _weak_dynamic_candidate(
            gain_pct=20.0,
            day_gain_pct=20.0,
            relative_volume=3.2,
            scanner_relative_volume=3.2,
            price_above_vwap=True,
            new_intraday_high=True,
            distance_from_vwap_pct=3.0,
        ),
        config={"broker": {"paper": False}, "dynamic_universe": {}},
        user_id="live_bot",
        spread_pct=0.2,
        spread_cap_pct=2.5,
    )

    assert decision["action"] == "allow"
    assert decision["reason"] == "exceptional_confirmation"


def test_strong_catalyst_dynamic_not_blocked_by_weak_late_cap() -> None:
    decision = _allocator_weak_catalyst_late_entry_decision(
        _weak_dynamic_candidate(gain_pct=25.0, day_gain_pct=25.0, news_score=8.0),
        config={"broker": {"paper": False}, "dynamic_universe": {}},
        user_id="live_bot",
        spread_pct=0.2,
        spread_cap_pct=2.5,
    )

    assert decision["action"] == "allow"
    assert decision["reason"] == "not_weak_catalyst_dynamic"


def test_weak_catalyst_late_guard_does_not_change_paper_behavior() -> None:
    decision = _allocator_weak_catalyst_late_entry_decision(
        _weak_dynamic_candidate(gain_pct=29.0, day_gain_pct=29.0),
        config={"broker": {"paper": True}, "dynamic_universe": {}},
        user_id="paper_bot",
        spread_pct=0.2,
        spread_cap_pct=2.5,
    )

    assert decision["action"] == "allow"
    assert decision["reason"] == "paper_context"


def _aggressive_row(**overrides) -> dict:
    row = _weak_dynamic_candidate(
        symbol="FCEL",
        gain_pct=10.0,
        day_gain_pct=10.0,
        relative_volume=2.0,
        scanner_relative_volume=2.0,
        price_above_vwap=True,
        new_intraday_high=True,
        spread_pct=0.2,
        route="dynamic_momentum_override",
        source="dynamic_universe",
    )
    row.update(overrides)
    return row


def _aggressive_config(**overrides) -> dict:
    cfg = {
        "broker": {"paper": False},
        "dynamic_aggressive": {
            "enabled_live": True,
            "max_positions": 1,
            "max_notional": 500,
            "min_gain_pct": 8.0,
            "max_gain_pct": 25.0,
            "min_relative_volume": 1.5,
            "require_vwap_above": True,
            "require_new_high_or_breakout": True,
            "max_spread_pct": 2.5,
        },
    }
    cfg["dynamic_aggressive"].update(overrides)
    return cfg


def test_dynamic_aggressive_disabled_creates_no_candidates(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _aggressive_config(enabled_live=False)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        out = build_dynamic_aggressive_scalp_candidates(
            [_aggressive_row()],
            config=cfg,
            user_id="live_bot",
        )

    assert out == []
    assert "DYNAMIC_AGGRESSIVE_REJECT symbol=FCEL reason=disabled" in caplog.text
    assert "DYNAMIC_AGGRESSIVE_SUMMARY candidates=1 accepted=0 rejected=1 orders=0" in caplog.text


def test_dynamic_aggressive_live_10pct_creates_500_notional_candidate(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        out = build_dynamic_aggressive_scalp_candidates(
            [_aggressive_row(gain_pct=10.0, day_gain_pct=10.0)],
            config=_aggressive_config(),
            user_id="live_bot",
        )

    assert len(out) == 1
    assert out[0]["route"] == "dynamic_aggressive_scalp"
    assert out[0]["source"] == "dynamic_aggressive"
    assert out[0]["notional"] == pytest.approx(500.0)
    assert "DYNAMIC_AGGRESSIVE_ACCEPT symbol=FCEL reason=ok" in caplog.text


def test_dynamic_aggressive_allows_20pct_when_safe_dynamic_late_guard_blocks() -> None:
    safe_decision = _allocator_weak_catalyst_late_entry_decision(
        _weak_dynamic_candidate(
            gain_pct=20.0,
            day_gain_pct=20.0,
            relative_volume=2.0,
            scanner_relative_volume=2.0,
            new_intraday_high=False,
        ),
        config={"broker": {"paper": False}, "dynamic_universe": {}},
        user_id="live_bot",
        spread_pct=0.2,
        spread_cap_pct=2.5,
    )
    aggressive = build_dynamic_aggressive_scalp_candidates(
        [
            _aggressive_row(
                gain_pct=20.0,
                day_gain_pct=20.0,
                relative_volume=2.0,
                scanner_relative_volume=2.0,
                new_intraday_high=True,
            )
        ],
        config=_aggressive_config(),
        user_id="live_bot",
    )

    assert safe_decision["action"] == "block"
    assert aggressive and aggressive[0]["notional"] == pytest.approx(500.0)


@pytest.mark.parametrize(
    ("row_update", "reason"),
    [
        ({"gain_pct": 26.0, "day_gain_pct": 26.0}, "gain_above_max"),
        ({"relative_volume": 1.4, "scanner_relative_volume": 1.4}, "relative_volume_below_min"),
        ({"price_above_vwap": False}, "price_not_above_vwap"),
        ({"new_intraday_high": False, "five_min_breakout": False}, "no_new_high_or_breakout"),
    ],
)
def test_dynamic_aggressive_reject_reasons(
    row_update: dict,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        out = build_dynamic_aggressive_scalp_candidates(
            [_aggressive_row(**row_update)],
            config=_aggressive_config(),
            user_id="live_bot",
        )

    assert out == []
    assert f"DYNAMIC_AGGRESSIVE_REJECT symbol=FCEL reason={reason}" in caplog.text


def test_dynamic_aggressive_max_positions_blocks_second_entry() -> None:
    out = build_dynamic_aggressive_scalp_candidates(
        [_aggressive_row(symbol="AIIO")],
        config=_aggressive_config(),
        user_id="live_bot",
        tracked={
            "FCEL": {
                "qty": 1,
                "route": "dynamic_aggressive_scalp",
                "source": "dynamic_aggressive",
            }
        },
    )

    assert out == []


def test_dynamic_aggressive_paper_unchanged_by_default() -> None:
    out = build_dynamic_aggressive_scalp_candidates(
        [_aggressive_row()],
        config={"broker": {"paper": True}, "dynamic_aggressive": {"enabled_live": True}},
        user_id="paper_bot",
    )

    assert out == []


def test_build_allocator_candidates_mrv_ranking_mode_sets_score() -> None:
    from src.signal_ranking import SIGNAL_RANKING_MODE_MRV

    c = build_allocator_candidates(
        [
            {
                "sym_u": "NVDA",
                "strength_eff": 0.99,
                "priority_score": 4.0,
                "rank_breakdown": {
                    "momentum": 0.3,
                    "relative_strength": 0.4,
                    "volume_signal": 0.5,
                },
            }
        ],
        ranking_mode=SIGNAL_RANKING_MODE_MRV,
    )
    assert len(c) == 1
    assert c[0]["score"] == pytest.approx(1.2)
    assert c[0]["strength_eff"] == pytest.approx(0.99)


def test_build_allocator_candidates_signal_objects_with_score_attr() -> None:
    class Sig:
        def __init__(self) -> None:
            self.symbol = "spy"
            self.score = 0.88

    c = build_allocator_candidates([Sig(), Sig()])
    assert len(c) == 2
    assert c[0]["symbol"] == "SPY"
    assert c[0]["score"] == pytest.approx(0.88)


def test_take_top_deploy_candidates_by_score() -> None:
    rows = [
        {"symbol": "A", "score": 1.0},
        {"symbol": "B", "score": 9.0},
        {"symbol": "C", "score": 5.0},
    ]
    out = take_top_deploy_candidates(rows, n=2)
    assert [r["symbol"] for r in out] == ["B", "C"]


def test_allocation_gap_report_logs_targets_and_actuals(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        payload = _log_allocation_gap_report(
            config={
                "portfolio": {
                    "target_core_stock_pct": 65,
                    "target_dynamic_pct": 25,
                    "target_cash_pct": 10,
                }
            },
            portfolio=[
                {"symbol": "AAPL", "value": 2_000.0},
                {"symbol": "MOBX", "value": 1_000.0, "dynamic_candidate": True},
            ],
            tracked={"MOBX": {"source": "dynamic_universe"}},
            equity=10_000.0,
            cash=7_000.0,
        )

    assert payload["target_core"] == pytest.approx(65.0)
    assert payload["actual_core"] == pytest.approx(20.0)
    assert payload["target_dynamic"] == pytest.approx(25.0)
    assert payload["actual_dynamic"] == pytest.approx(10.0)
    assert payload["cash_pct"] == pytest.approx(70.0)
    assert "ALLOCATION_TARGETS_DETAIL core_target_pct=65.00 dynamic_target_pct=25.00 cash_reserve_pct=10.00" in caplog.text
    assert "ALLOCATION_ACTUALS core_pct=20.00 dynamic_pct=10.00 cash_pct=70.00" in caplog.text
    assert "ALLOCATION_GAP_REPORT target_core=65.00 actual_core=20.00 target_dynamic=25.00 actual_dynamic=10.00 cash_pct=70.00" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_deploy_mode_passes_top_n_candidates_to_allocate(
    mock_allocator_cls: MagicMock, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Deploy mode: only top *N* (3–5) candidates by score are passed to ``allocate``."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "A", "composite_score": 1.0},
                {"sym_u": "B", "composite_score": 9.0},
                {"sym_u": "C", "composite_score": 5.0},
                {"sym_u": "D", "composite_score": 2.0},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )
    out = capsys.readouterr().out
    assert "ALLOCATOR mode: deploy" in out
    kw = inst.allocate.call_args.kwargs
    cands = kw.get("candidates", [])
    assert len(cands) == 3
    syms = {c["symbol"] for c in cands}
    assert syms == {"B", "C", "D"}
    assert "deploy mode" in caplog.text.lower()


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_logs_dynamic_candidate_removed_before_ranking(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=35.0, spread_pct=0.1)

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "CMND",
                    "score": 4.2,
                    "dynamic_candidate": True,
                    "news_score": 0,
                    "event_score": 0,
                    "catalyst_score": 0,
                    "age_minutes": 17,
                    "route": "dynamic_scan",
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "candidates: 1" in out
    assert "ranked: []" in out
    assert "selected: []" in out
    assert "reject_reasons: ['CMND:no_catalyst']" in out
    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert "ALLOCATOR_STAGE_COUNT allocator_input_count=1" in caplog.text
    assert (
        "ALLOCATOR_CANDIDATE_ROW stage=before_filter symbol=CMND is_dynamic=true "
        "news_score=0.0 catalyst_score=0.0 event_score=0.0"
    ) in caplog.text
    assert "ALLOCATOR_STAGE_COUNT post_profile_filter_count=0" in caplog.text
    assert "ALLOCATOR_STAGE_COUNT post_ranking_count=0" in caplog.text
    assert (
        "ALLOCATOR_FILTER_REJECT symbol=CMND reason=no_catalyst "
        "score=5.670000000000001 catalyst_score=0.0 event_score=0.0 "
        "news_score=0.0 age_minutes=17.0 route=dynamic_scan"
    ) in caplog.text
    assert (
        "ALLOCATOR_REJECT_REASON symbol=CMND reason=no_catalyst stage=profile_filter"
        in caplog.text
    )
    assert "ALLOCATOR_DROPPED symbol=CMND reason=no_catalyst" in caplog.text
    assert "reason skipped: no_candidates_after_selection" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_dynamic_abat_final_true_removed_before_ranking_logs_exact_reason(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=35.0, spread_pct=0.1)

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "ABAT",
                    "source": "dynamic_universe",
                    "route": "dynamic_universe",
                    "dynamic_symbol": True,
                    "entry_eval_final": True,
                    "score": 4.2,
                    "signal_score": 78.0,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                    "relative_volume": 1.76,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "candidates: 1" in out
    assert "ranked: []" in out
    assert "selected: []" in out
    assert "reject_reasons: ['ABAT:no_catalyst']" in out
    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert (
        "ALLOCATOR_CANDIDATE_ROW stage=before_filter symbol=ABAT is_dynamic=true "
        "news_score=0.0 catalyst_score=0.0 event_score=0.0 signal_score=78.0 "
        "relative_volume=1.76 allocation_bucket=dynamic"
    ) in caplog.text
    assert (
        "ALLOCATOR_REJECT_REASON symbol=ABAT reason=no_catalyst stage=profile_filter"
        in caplog.text
    )
    assert "ALLOCATOR_DROPPED symbol=ABAT reason=no_catalyst" in caplog.text
    assert "reason skipped: no_candidates_after_selection" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_dynamic_override_pure_momentum_reaches_allocator_without_no_catalyst(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=35.0, spread_pct=0.1)

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "INTC",
                    "source": "dynamic_universe",
                    "route": "dynamic_momentum_override",
                    "dynamic_symbol": True,
                    "dynamic_candidate": True,
                    "is_dynamic": True,
                    "entry_eval_final": True,
                    "score": 1.2,
                    "strength_eff": 1.2,
                    "dynamic_score": 40.0,
                    "scanner_score": 40.0,
                    "signal_score": 40.0,
                    "gain_pct": 4.0,
                    "day_gain_pct": 4.0,
                    "relative_volume": 1.0,
                    "rel_volume": 1.0,
                    "spread_pct": 0.1,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "ranked: ['INTC']" in out
    assert "selected: ['INTC']" in out
    assert "INTC:no_catalyst" not in out
    assert inst.allocate.call_args.kwargs["candidates"][0]["symbol"] == "INTC"
    assert (
        "DYNAMIC_ALLOCATOR_INPUT symbol=INTC route=dynamic_momentum_override "
        "source=dynamic_universe score=40.00 gain=4.000 rel=1.000 "
        "catalyst_score=0.00 news_score=0.00 event_score=0.00"
    ) in caplog.text
    assert "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=INTC score=40.00 rel=1.000 gain=4.000" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_dynamic_override_low_score_still_gets_no_catalyst_reject_reason(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=35.0, spread_pct=0.1)

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "INTC",
                    "source": "dynamic_universe",
                    "route": "dynamic_momentum_override",
                    "dynamic_symbol": True,
                    "dynamic_candidate": True,
                    "is_dynamic": True,
                    "entry_eval_final": True,
                    "score": 1.2,
                    "strength_eff": 1.2,
                    "dynamic_score": 20.0,
                    "scanner_score": 20.0,
                    "signal_score": 20.0,
                    "gain_pct": 4.0,
                    "relative_volume": 1.0,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "reject_reasons: ['INTC:no_catalyst']" in out
    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert (
        "DYNAMIC_ALLOCATOR_INPUT symbol=INTC route=dynamic_momentum_override "
        "source=dynamic_universe score=20.00 gain=4.000 rel=1.000 "
        "catalyst_score=0.00 news_score=0.00 event_score=0.00"
    ) in caplog.text
    assert "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=INTC score=20.00 rel=1.000 gain=4.000 required_score=35.00" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_nvda_dynamic_override_low_score_reproduces_no_catalyst_reject_reason(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=500.0, spread_pct=0.1)

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "NVDA",
                    "source": "dynamic_universe",
                    "route": "dynamic_momentum_override",
                    "dynamic_symbol": True,
                    "dynamic_candidate": True,
                    "is_dynamic": True,
                    "entry_eval_final": True,
                    "score": 1.2,
                    "strength_eff": 1.2,
                    "dynamic_score": 20.0,
                    "scanner_score": 20.0,
                    "signal_score": 20.0,
                    "gain_pct": 4.0,
                    "day_gain_pct": 4.0,
                    "relative_volume": 1.0,
                    "rel_volume": 1.0,
                    "spread_pct": 0.1,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "reject_reasons: ['NVDA:no_catalyst']" in out
    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert (
        "DYNAMIC_ALLOCATOR_INPUT symbol=NVDA route=dynamic_momentum_override "
        "source=dynamic_universe score=20.00 gain=4.000 rel=1.000 "
        "catalyst_score=0.00 news_score=0.00 event_score=0.00"
    ) in caplog.text
    assert "DYNAMIC_ALLOCATOR_NO_CATALYST_REJECT symbol=NVDA score=20.00 rel=1.000 gain=4.000 required_score=35.00" in caplog.text
    assert (
        "ALLOCATOR_DROP_REASON_DEBUG symbol=NVDA reason=no_catalyst "
        "route=dynamic_momentum_override source=dynamic_universe is_dynamic=true"
    ) in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_nvda_dynamic_override_high_quality_pure_momentum_avoids_no_catalyst(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=500.0, spread_pct=0.1)

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "NVDA",
                    "source": "dynamic_universe",
                    "route": "dynamic_momentum_override",
                    "dynamic_symbol": True,
                    "dynamic_candidate": True,
                    "is_dynamic": True,
                    "entry_eval_final": True,
                    "score": 1.2,
                    "strength_eff": 1.2,
                    "dynamic_score": 40.0,
                    "scanner_score": 40.0,
                    "signal_score": 40.0,
                    "gain_pct": 4.0,
                    "day_gain_pct": 4.0,
                    "relative_volume": 1.0,
                    "rel_volume": 1.0,
                    "spread_pct": 0.1,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "NVDA:no_catalyst" not in out
    assert inst.allocate.call_args.kwargs["candidates"][0]["symbol"] == "NVDA"
    assert (
        "DYNAMIC_ALLOCATOR_INPUT symbol=NVDA route=dynamic_momentum_override "
        "source=dynamic_universe score=40.00 gain=4.000 rel=1.000 "
        "catalyst_score=0.00 news_score=0.00 event_score=0.00"
    ) in caplog.text
    assert "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=NVDA score=40.00 rel=1.000 gain=4.000" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_nvda_final_reject_print_path_removes_stale_no_catalyst(
    mock_allocator_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=500.0, spread_pct=0.1)

    def _stale_no_catalyst_rejects(*_args: object, **_kwargs: object) -> dict[str, str]:
        return {"NVDA": "no_catalyst"}

    monkeypatch.setattr(
        "src.capital_allocator_loop._log_allocator_filter_rejections",
        _stale_no_catalyst_rejects,
    )

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "NVDA",
                    "source": "dynamic_universe",
                    "route": "dynamic_momentum_override",
                    "dynamic_symbol": True,
                    "dynamic_candidate": True,
                    "is_dynamic": True,
                    "entry_eval_final": True,
                    "score": 1.2,
                    "strength_eff": 1.2,
                    "dynamic_score": 40.0,
                    "scanner_score": 40.0,
                    "signal_score": 40.0,
                    "gain_pct": 4.0,
                    "day_gain_pct": 4.0,
                    "relative_volume": 1.0,
                    "rel_volume": 1.0,
                    "spread_pct": 0.1,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "NVDA:no_catalyst" not in out
    assert (
        "ALLOCATOR_FINAL_REJECT_REASON_DEBUG symbol=NVDA reason=no_catalyst "
        "candidate_present=true route=dynamic_momentum_override source=dynamic_universe "
        "is_dynamic=true"
    ) in caplog.text
    assert "ALLOCATOR_FINAL_REJECT_REASON_BYPASS symbol=NVDA reason=no_catalyst path=pure_momentum" in caplog.text
    assert "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=NVDA score=40.00 rel=1.000 gain=4.000" in caplog.text
    assert inst.allocate.call_args.kwargs["candidates"][0]["symbol"] == "NVDA"


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_nvda_dynamic_override_final_true_cannot_keep_no_catalyst_reject(
    mock_allocator_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=500.0, spread_pct=0.1)

    monkeypatch.setattr(
        "src.capital_allocator_loop._log_allocator_filter_rejections",
        lambda *_args, **_kwargs: {"NVDA": "no_catalyst"},
    )

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "NVDA",
                    "source": "dynamic_universe",
                    "route": "dynamic_momentum_override",
                    "dynamic_symbol": True,
                    "dynamic_candidate": True,
                    "is_dynamic": True,
                    "entry_eval_final": True,
                    "final": True,
                    "score": 1.2,
                    "strength_eff": 1.2,
                    "dynamic_score": 39.1,
                    "scanner_score": 39.1,
                    "signal_score": 39.1,
                    "gain_pct": 31.4,
                    "day_gain_pct": 31.4,
                    "relative_volume": 2.67,
                    "rel_volume": 2.67,
                    "spread_pct": 0.1,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "NVDA:no_catalyst" not in out
    assert (
        "ALLOCATOR_FINAL_REJECT_REASON_DEBUG symbol=NVDA reason=no_catalyst "
        "candidate_present=true route=dynamic_momentum_override source=dynamic_universe "
        "is_dynamic=true"
    ) in caplog.text
    assert "ALLOCATOR_FINAL_REJECT_REASON_BYPASS symbol=NVDA reason=no_catalyst path=pure_momentum" in caplog.text
    assert "DYNAMIC_OVERRIDE_NO_CATALYST_THRESHOLD_FAIL symbol=NVDA" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_btq_dynamic_override_live_shape_does_not_drop_no_catalyst(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote.return_value = _Quote(mid=6.0, spread_pct=0.1)

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "BTQ",
                    "source": "dynamic_universe",
                    "route": "dynamic_momentum_override",
                    "dynamic_symbol": True,
                    "dynamic_candidate": True,
                    "is_dynamic": True,
                    "entry_eval_final": True,
                    "score": 1.2,
                    "strength_eff": 1.2,
                    "dynamic_score": 39.10,
                    "scanner_score": 39.10,
                    "signal_score": 39.10,
                    "gain_pct": 31.4,
                    "day_gain_pct": 31.4,
                    "relative_volume": 2.67,
                    "rel_volume": 2.67,
                    "spread_pct": 0.1,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                }
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "BTQ:no_catalyst" not in out
    assert inst.allocate.call_args.kwargs["candidates"][0]["symbol"] == "BTQ"
    assert (
        "DYNAMIC_ALLOCATOR_INPUT symbol=BTQ route=dynamic_momentum_override "
        "source=dynamic_universe score=39.10 gain=31.400 rel=2.670 "
        "catalyst_score=0.00 news_score=0.00 event_score=0.00"
    ) in caplog.text
    assert "DYNAMIC_ALLOCATOR_PURE_MOMENTUM_PASS symbol=BTQ score=39.10 rel=2.670 gain=31.400" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_entry_eval_final_candidate_drop_has_reject_reason_or_reaches_ranking(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "ABAT",
                    "source": "dynamic_universe",
                    "route": "dynamic_universe",
                    "dynamic_symbol": True,
                    "entry_eval_final": True,
                    "score": 4.2,
                    "signal_score": 78.0,
                    "news_score": 0.0,
                    "event_score": 0.0,
                    "catalyst_score": 0.0,
                    "relative_volume": 1.76,
                }
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    ranked_or_selected = "ranked: ['ABAT']" in out or "selected: ['ABAT']" in out
    reject_logged = (
        "ALLOCATOR_REJECT_REASON symbol=ABAT" in caplog.text
        and "reject_reasons: ['ABAT:no_catalyst']" in out
    )
    assert ranked_or_selected or reject_logged


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_dynamic_catalyst_candidate_reaches_allocator_ranking(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "ABAT",
                    "score": 4.2,
                    "source": "dynamic_universe",
                    "route": "dynamic_universe",
                    "dynamic_candidate": True,
                    "entry_eval_final": True,
                    "news_score": 0,
                    "event_score": 0,
                    "catalyst_score": 0.8,
                    "catalyst_age_minutes": 22,
                    "relative_volume": 1.76,
                }
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0.85,
                "deploy_top_n_signals": 3,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "candidates: 1" in out
    assert "ranked: ['ABAT']" in out
    assert "selected: ['ABAT']" in out
    cands = inst.allocate.call_args.kwargs["candidates"]
    assert [row["symbol"] for row in cands] == ["ABAT"]
    assert cands[0]["catalyst_score"] == pytest.approx(0.8)
    assert "ALLOCATOR_INPUT count=1" in caplog.text
    assert "ALLOCATOR_INPUT_SYMBOLS count=1 symbols=ABAT" in caplog.text
    assert "ALLOCATOR_RANKED_SYMBOLS count=1 symbols=ABAT" in caplog.text
    assert "ALLOCATOR_SELECTED_SYMBOLS count=1 symbols=ABAT" in caplog.text
    assert "ALLOCATOR_REJECT_REASON symbol=ABAT" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_pass_attempts_allocator_candidates_in_ranked_order(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "ORCL", "score": 1.0, "route": "core_rebuild", "core_rebuild": True},
                {
                    "sym_u": "GOOGL",
                    "score": 10.0,
                    "dynamic_candidate": True,
                    "allocation_bucket": "dynamic",
                    "news_score": 4.0,
                    "event_score": 4.0,
                    "catalyst_score": 0.4,
                    "route": "premarket_catalyst_replay",
                    "candidate_notional_requested": 1_312.50,
                },
                {"sym_u": "AMD", "score": 5.0, "route": "core_rebuild", "core_rebuild": True},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={
                "options": {"enabled": False},
                "portfolio": {
                    "target_cash_pct": 0,
                    "dynamic_quality": {
                        "enabled": True,
                        "allow_event_news_fallback": True,
                        "min_catalyst_score": 0.3,
                        "min_event_score": 3.0,
                        "min_news_score": 3.0,
                    },
                },
            },
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "min_gross_deployment_pct": 0,
                "force_minimum_trade_single_candidate": False,
                "selected_must_execute": False,
                "fallback_on_empty_alloc": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "ranked: ['GOOGL', 'AMD', 'ORCL']" in out
    assert "selected: ['GOOGL', 'AMD', 'ORCL']" in out
    cands = inst.allocate.call_args.kwargs["candidates"]
    assert [row["symbol"] for row in cands] == ["GOOGL", "AMD", "ORCL"]
    assert "ALLOCATOR_RANKED_SYMBOLS count=3 symbols=GOOGL,AMD,ORCL" in caplog.text
    assert "ALLOCATOR_SELECTED_SYMBOLS count=3 symbols=GOOGL,AMD,ORCL" in caplog.text
    assert "ALLOCATOR_POST_RANK_REORDER" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_max_gross_increase_per_cycle_clamps_allocate_cap(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """effective max gross USD sent to allocate() is min(book cap, prior gross + equity × increase)."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "symbol_caps": {},
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "max_gross_increase_per_cycle": 0.05,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )
    kw = inst.allocate.call_args.kwargs
    # Book cap ~92k equity × default max exposure; cycle ceiling = 50k + 5% equity = 55k.
    assert kw["max_total_gross_dollars"] == pytest.approx(55_000.0)
    assert kw["current_gross_dollars"] == pytest.approx(50_000.0)
    assert "max_gross_increase_per_cycle caps" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_controlled_live_allocator_caps_adaptive_bullish_headroom(
    mock_allocator_cls: MagicMock,
    tmp_path: Path,
) -> None:
    """Controlled-live allocator headroom cannot exceed the advertised 85% portfolio cap."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    execute_capital_allocator_pass(
        signals=[],
        broker=MagicMock(),
        engine=MagicMock(),
        config={
            "options": {"enabled": False},
            "trading_control": {
                "mode": "live",
                "runtime_profile": "controlled_live_equity",
                "controlled_live_equity": {
                    "enabled": True,
                    "portfolio_exposure_cap_pct": 85,
                    "max_managed_positions": 10,
                    "max_single_order_notional_pct": 0.12,
                    "max_single_order_notional": 5000,
                    "max_symbol_exposure_pct": 15,
                    "strategy_allocation_cap_pct": 60,
                    "stock_capital_pct": 60,
                    "min_cash_reserve_pct": 12,
                    "daily_loss_limit_pct": 3,
                },
            },
            "portfolio": {"exposure_gates": {"enabled": True, "max_total_exposure_frac": 0.95}},
            "adaptive": {
                "max_exposure_by_regime": {"bullish": 0.98, "neutral": 0.95, "bearish": 0.85},
                "bullish_score_4_plus_max_exposure_frac": 1.20,
                "max_exposure_frac_ceiling": 1.35,
            },
        },
        dt=MagicMock(strftime=MagicMock(return_value="10:00")),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "symbol_caps": {},
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
        },
        user_id="u1",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
        allow_allocator_buys=True,
        gross_exposure_pct=50.0,
        regime_score=5,
        regime_condition="bullish",
    )

    kw = inst.allocate.call_args.kwargs
    assert kw["max_total_gross_dollars"] == pytest.approx(85_000.0)
    assert kw["current_gross_dollars"] == pytest.approx(50_000.0)


def test_build_allocator_candidates_uses_zero_score_without_rank_fields() -> None:
    """Step 1 includes every signal row; missing score keys → 0.0."""
    c = build_allocator_candidates([{"sym_u": "ZZZ", "final": True}])
    assert len(c) == 1
    assert c[0]["symbol"] == "ZZZ"
    assert c[0]["score"] == pytest.approx(0.0)


def test_execute_capital_allocator_pass_prints_allocator_actions(capsys, tmp_path: Path) -> None:
    """Confirms the post-allocate print: ALLOCATOR ACTIONS plus the actions list (empty [])."""
    broker = MagicMock()
    engine = MagicMock()
    config = {"options": {"enabled": False}}
    positions: list[dict[str, float]] = []
    tracked: dict[str, object] = {}
    current: dict[str, object] = {}
    ca_cfg = {
        "max_positions": 5,
        "symbol_cap": 0.25,
        "min_trade_size": 500.0,
        "min_realloc_leg": 300.0,
        "rotate_trim_fraction": 0.3,
    }
    dt = MagicMock()
    dt.strftime = MagicMock(return_value="09:30")
    execute_capital_allocator_pass(
        signals=[],
        broker=broker,
        engine=engine,
        config=config,
        dt=dt,
        positions=positions,
        tracked=tracked,
        current_positions=current,
        eligible_active=[],
        account_equity=100_000.0,
        cash=0.0,
        ca_cfg=ca_cfg,
        user_id="u1",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
    )
    out = capsys.readouterr().out
    assert any(
        line == "ALLOCATOR ACTIONS: []"
        for line in out.splitlines()
    ), f"expected print of empty actions; got stdout: {out!r}"


def _allocator_dispatch_common_kwargs(tmp_path: Path) -> dict:
    broker = MagicMock()
    broker.get_latest_quote = MagicMock(return_value=_Quote(mid=30.0, spread_pct=0.1))
    broker.get_positions = MagicMock(return_value=[])
    broker.get_buying_power = MagicMock(return_value=10_000.0)
    engine = MagicMock()
    engine.market_quality = MagicMock()
    engine.market_quality.should_ignore_spread_for_low_volume = MagicMock(return_value=False)
    engine.execution.last_order_build_reject_reason = None
    engine.strategy.stop_loss_pct = 1.5
    engine.strategy.take_profit_pct = 3.0
    engine.strategy.time_bars_exit = 20
    return {
        "signals": [
            {
                "sym_u": "INTC",
                "symbol": "INTC",
                "score": 2.0,
                "strength_eff": 2.0,
                "notional": 4000.0068,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
                "dynamic_symbol": True,
                "news_score": 10.0,
                "catalyst_score": 0.91,
                "relative_volume": 2.0,
            }
        ],
        "broker": broker,
        "engine": engine,
        "config": {
            "options": {"enabled": False},
            "dynamic_universe": {"min_relative_volume": 1.0},
        },
        "dt": datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
        "positions": [],
        "tracked": {},
        "current_positions": {},
        "eligible_active": [],
        "account_equity": 100_000.0,
        "cash": 25_000.0,
        "ca_cfg": {
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "require_net_sell_gte_buy": False,
        },
        "user_id": "paper_bot",
        "data_dir": tmp_path,
        "stale_quote_max_age": 60.0,
        "strength_jitter_max": 0.0,
        "et_date_iso": "2026-06-11",
        "cycle_risk_state": None,
        "verbose": False,
        "allow_allocator_buys": True,
        "gross_exposure_pct": 10.0,
    }


def _dynamic_allocator_action(symbol: str = "INTC", notional: float = 1200.0) -> dict:
    return {
        "action": "buy",
        "symbol": symbol,
        "notional": notional,
        "source": "dynamic_universe",
        "route": "dynamic_momentum_override",
        "dynamic_candidate": True,
    }


def _paper_dynamic_signal(symbol: str = "INTC", *, score: float = 45.0) -> dict:
    return {
        "sym_u": symbol,
        "symbol": symbol,
        "score": score,
        "strength_eff": score,
        "scanner_score": score,
        "dynamic_score": score,
        "signal_score": score,
        "source": "dynamic_universe",
        "route": "dynamic_momentum_override",
        "dynamic_candidate": True,
        "dynamic_symbol": True,
        "entry_eval_final": True,
        "decision_allowed": True,
        "relative_volume": 1.4,
        "rel_volume": 1.4,
        "gain_pct": 4.5,
        "news_score": 9.0,
        "event_score": 8.0,
        "catalyst_score": 0.9,
    }


def _write_signal_expectancy_report(
    data_dir: Path,
    *,
    day: str,
    symbol: str = "INTC",
    route: str = "dynamic_momentum_override",
    count: int = 6,
    score: float = -0.75,
) -> None:
    path = data_dir / "research_metrics" / day / "signal_expectancy_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "report": "signal_expectancy_report",
        "date": day,
        "route_expectancy": [
            {"route": route, "count": count, "expectancy_score": score},
        ],
        "symbol_expectancy": [
            {"symbol": symbol, "route": route, "count": count, "expectancy_score": score},
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _trend_action(symbol: str = "JPM", *, action: str = "buy", notional: float = 1200.0) -> dict:
    return {
        "action": action,
        "symbol": symbol,
        "notional": notional,
        "source": "scoring",
        "route": "trend_long",
    }


def _trend_signal(symbol: str = "JPM", **updates: object) -> dict:
    row = {
        "sym_u": symbol,
        "symbol": symbol,
        "score": 5.0,
        "strength_eff": 5.0,
        "source": "scoring",
        "route": "trend_long",
        "signal_timestamp": "2026-06-11T10:10:00+00:00",
    }
    row.update(updates)
    return row


def _record_trend_stop(data_dir: Path, *, symbol: str = "JPM", timestamp: datetime) -> None:
    record_exit(
        data_dir=data_dir,
        user_id="live_bot",
        timestamp=timestamp,
        symbol=symbol,
        exit_reason="stop_loss",
        pnl=-25.0,
        entry_route="trend_long",
        entry_source="scoring",
    )


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_repeated_reversal_blocked(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_dynamic_allocator_action("ILLR", 1400.0)])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [_paper_dynamic_signal("ILLR")]
    kwargs["dt"] = datetime(2026, 6, 25, 15, 0, tzinfo=timezone.utc)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {
            "min_relative_volume": 1.0,
            "paper_churn_guard": {"flip_window_minutes": 60, "max_reversals_in_window": 1},
        },
    }
    record_order_event(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=kwargs["dt"] - timedelta(minutes=40),
        symbol="ILLR",
        action="buy",
        route="dynamic_momentum_override",
        source="dynamic_universe",
        submitted=True,
        dynamic_candidate=True,
    )
    record_exit(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=kwargs["dt"] - timedelta(minutes=20),
        symbol="ILLR",
        exit_reason="signal_flip",
        pnl=-45.0,
        entry_route="dynamic_momentum_override",
        entry_source="dynamic_universe",
    )

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "REVERSAL_GUARD_BLOCK symbol=ILLR" in caplog.text
    assert "CHURN_GUARD_BLOCK symbol=ILLR reason=reversal_guard" in caplog.text
    assert "ORDER_SKIP symbol=ILLR reason=reversal_guard" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_reentry_after_weak_exit_blocked(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_dynamic_allocator_action("BTQ", 1400.0)])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [_paper_dynamic_signal("BTQ", score=45.0)]
    kwargs["dt"] = datetime(2026, 6, 25, 15, 0, tzinfo=timezone.utc)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {
            "min_relative_volume": 1.0,
            "paper_churn_guard": {
                "weak_exit_reentry_cooldown_minutes": 180,
                "fresh_score_delta": 10,
            },
        },
    }
    record_exit(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=kwargs["dt"] - timedelta(minutes=30),
        symbol="BTQ",
        exit_reason="weak_exit",
        pnl=-35.0,
        entry_route="dynamic_momentum_override",
        entry_source="dynamic_universe",
    )

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "REENTRY_AFTER_WEAK_EXIT_BLOCKED symbol=BTQ exit_reason=weak_exit" in caplog.text
    assert "CHURN_GUARD_BLOCK symbol=BTQ reason=reentry_after_weak_exit" in caplog.text
    assert "ORDER_SKIP symbol=BTQ reason=reentry_after_weak_exit" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_symbol_daily_loss_guard_blocks_new_entries(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_dynamic_allocator_action("ILLR", 1400.0)])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [_paper_dynamic_signal("ILLR", score=70.0)]
    kwargs["dt"] = datetime(2026, 6, 25, 15, 0, tzinfo=timezone.utc)
    kwargs["account_equity"] = 100_000.0
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 1.0},
    }
    record_exit(
        data_dir=tmp_path,
        user_id="paper_bot",
        timestamp=kwargs["dt"] - timedelta(minutes=10),
        symbol="ILLR",
        exit_reason="stop_loss",
        pnl=-717.15,
        entry_route="dynamic_momentum_override",
        entry_source="dynamic_universe",
    )

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "SYMBOL_DAILY_LOSS_GUARD_BLOCK symbol=ILLR realized_loss=717.15 threshold=250.00" in caplog.text
    assert "CHURN_GUARD_BLOCK symbol=ILLR reason=symbol_daily_loss_guard" in caplog.text
    assert "ORDER_SKIP symbol=ILLR reason=symbol_daily_loss_guard" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_hard_low_avg_volume_excluded_before_allocator_action(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "ADIL", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "ADIL",
            "symbol": "ADIL",
            "score": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "catalyst_score": 0.82,
            "avg_volume": 1950,
            "relative_volume": 2.0,
        }
    ]
    kwargs["config"]["dynamic_universe"] = {
        "min_avg_volume": 10_000,
        "min_relative_volume": 1.0,
        "min_price": 2.0,
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    inst.allocate.assert_not_called()
    assert "ALLOCATOR_CANDIDATE_REJECT symbol=ADIL reason=hard_liquidity_gate" in caplog.text
    assert "below_min_avg_volume avg=1950 min=10000" in caplog.text
    assert "ALLOCATOR_ACTION_CREATED symbol=ADIL" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_no_quote_blocks_subsequent_allocator_cycles(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "ADIL", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["broker"].get_latest_quote.return_value = None
    kwargs["signals"] = [
        {
            "sym_u": "ADIL",
            "symbol": "ADIL",
            "score": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "avg_volume": 50_000,
            "price": 10.0,
            "relative_volume": 2.0,
        }
    ]
    kwargs["config"]["dynamic_universe"] = {
        "min_avg_volume": 10_000,
        "min_relative_volume": 1.0,
        "min_price": 2.0,
    }
    kwargs["ca_cfg"] = {**kwargs["ca_cfg"], "illiquid_symbol_block_ttl_min": 7}

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)
        execute_capital_allocator_pass(**kwargs)

    assert "ALLOCATOR_SYMBOL_BLOCKED symbol=ADIL reason=no_quote ttl_min=7" in caplog.text
    assert "ALLOCATOR_CANDIDATE_REJECT symbol=ADIL reason=hard_liquidity_gate detail=no_quote" in caplog.text
    assert (
        "ALLOCATOR_CANDIDATE_REJECT symbol=ADIL reason=hard_liquidity_gate detail=blocked_after_no_quote"
        in caplog.text
    )
    assert "ALLOCATOR_SKIP symbol=ADIL reason=blocked_after_no_quote" in caplog.text
    inst.allocate.assert_not_called()


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_strong_catalyst_valid_liquidity_still_reaches_allocator(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "GOOD",
            "symbol": "GOOD",
            "score": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "catalyst_score": 0.82,
            "news_score": 9.0,
            "avg_volume": 250_000,
            "price": 10.0,
            "spread_pct": 0.2,
            "relative_volume": 2.0,
        }
    ]
    kwargs["config"]["dynamic_universe"] = {
        "min_avg_volume": 10_000,
        "min_relative_volume": 1.0,
        "min_price": 2.0,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    inst.allocate.assert_called_once()
    assert "ALLOCATOR_INPUT_SYMBOLS count=1 symbols=GOOD" in caplog.text
    assert "ALLOCATOR_CANDIDATE_REJECT symbol=GOOD" not in caplog.text


def _scanner_selected_dynamic_signal(symbol: str = "HIVE") -> dict:
    return {
        "sym_u": symbol,
        "symbol": symbol,
        "score": 2.0,
        "strength_eff": 2.0,
        "source": "dynamic_universe",
        "route": "dynamic_momentum_override",
        "dynamic_candidate": True,
        "dynamic_symbol": True,
        "is_dynamic": True,
        "scanner_selected": True,
        "entry_eval_final": True,
        "avg_volume": 250_000,
        "price": 10.0,
        "spread_pct": 0.2,
        "relative_volume": 0.8,
        "entry_eval_effective_min_rel_volume": 0.3,
        "day_gain_pct": 4.2,
        "price_above_vwap": True,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
    }


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_scanner_selected_dynamic_no_catalyst_reaches_allocator_action(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "HIVE", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [_scanner_selected_dynamic_signal("HIVE")]
    kwargs["config"]["dynamic_universe"] = {
        "min_avg_volume": 10_000,
        "min_relative_volume": 0.3,
        "min_price": 2.0,
    }
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    inst.allocate.assert_called_once()
    assert inst.allocate.call_args.kwargs["candidates"][0]["symbol"] == "HIVE"
    assert "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=HIVE reason=scanner_selected" in caplog.text
    assert "HIVE:no_catalyst" not in caplog.text
    assert "ALLOCATOR_ACTION_CREATED symbol=HIVE action=buy notional=1200.00 route=dynamic_momentum_override" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_weak_catalyst_dynamic_requires_stronger_rvol_confirmation(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "DFTX", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("DFTX")
    signal.update(
        {
            "relative_volume": 0.40,
            "entry_eval_effective_min_rel_volume": 0.3,
            "scanner_score": 55.0,
            "price_above_vwap": True,
            "news_score": 0.0,
            "event_score": 0.0,
            "catalyst_score": 0.0,
        }
    )
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [signal]
    kwargs["config"]["dynamic_universe"] = {"min_relative_volume": 0.3, "min_price": 2.0}
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    inst.allocate.assert_called_once()
    assert "DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=DFTX" in caplog.text
    assert "DYNAMIC_WEAK_CATALYST_REJECT symbol=DFTX reason=relative_volume_below_0.50" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=DFTX" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_weak_catalyst_dynamic_size_is_reduced(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "SOFI", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("SOFI")
    signal.update(
        {
            "relative_volume": 0.70,
            "scanner_score": 55.0,
            "price_above_vwap": True,
            "news_score": 0.0,
            "event_score": 0.0,
            "catalyst_score": 0.0,
        }
    )
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [signal]
    kwargs["config"]["dynamic_universe"] = {"min_relative_volume": 0.3, "min_price": 2.0}
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=SOFI" in caplog.text
    assert (
        "DYNAMIC_WEAK_CATALYST_SIZE_REDUCED symbol=SOFI original_notional=2000.00 "
        "reduced_notional=600.00"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=SOFI action=buy notional=600.00" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_weak_catalyst_dynamic_blocks_zero_news_non_exceptional(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import (
        _ALLOCATOR_SYMBOL_BLOCK_UNTIL,
        _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL,
    )

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "MU", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("MU")
    signal.update(
        {
            "relative_volume": 0.70,
            "scanner_score": 55.0,
            "gain_pct": 2.5,
            "price_above_vwap": True,
            "news_score": 0.0,
            "event_score": 0.0,
            "catalyst_score": 0.0,
        }
    )
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [signal]
    kwargs["config"]["broker"] = {"paper": False}
    kwargs["config"]["dynamic_universe"] = {"min_relative_volume": 0.3, "min_price": 2.0}
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "WEAK_CATALYST_DYNAMIC_BLOCKED symbol=MU reason=non_exceptional_zero_news_live" in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=MU reason=weak_catalyst_dynamic_non_exceptional_live minutes=10" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=MU" not in caplog.text


def _enable_live_weak_catalyst_exception(config: dict) -> None:
    config.setdefault("dynamic_universe", {}).update(
        {
            "min_relative_volume": 0.3,
            "min_price": 2.0,
            "live_weak_catalyst_exception_experiment": {
                "enabled": True,
                "min_price": 8,
                "min_gain_pct": 10,
                "min_relative_volume": 0.5,
                "max_spread_pct": 0.25,
                "require_entry_eval_pass": True,
                "max_atr_pct": 15,
                "max_positions_per_day": 1,
                "notional_cap": 300,
                "require_no_existing_position": True,
            },
        }
    )


def _live_weak_exception_signal(symbol: str = "RIVN") -> dict:
    signal = _scanner_selected_dynamic_signal(symbol)
    signal.update(
        {
            "price": 10.0,
            "paper_current_price": 10.0,
            "relative_volume": 0.7,
            "scanner_score": 55.0,
            "gain_pct": 12.0,
            "day_gain_pct": 12.0,
            "spread_pct": 0.2,
            "atr_pct": 5.0,
            "price_above_vwap": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "news_score": 0.0,
            "event_score": 0.0,
            "catalyst_score": 0.0,
        }
    )
    return signal


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_weak_catalyst_exception_enabled_allows_qualifying_candidate_and_caps_notional(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import (
        _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL,
        _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS,
    )

    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "RIVN", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="rivn-exp")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [_live_weak_exception_signal("RIVN")]
    kwargs["config"]["broker"] = {"paper": False}
    _enable_live_weak_catalyst_exception(kwargs["config"])
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "LIVE_WEAK_CATALYST_EXCEPTION_CHECK symbol=RIVN enabled=true" in caplog.text
    assert "LIVE_WEAK_CATALYST_EXCEPTION_ALLOW symbol=RIVN reason=ok notional=1200.00 cap=300.00" in caplog.text
    assert "LIVE_WEAK_CATALYST_EXCEPTION_CAP symbol=RIVN original_notional=1200.00 capped_notional=300.00 cap=300.00" in caplog.text
    assert "WEAK_CATALYST_DYNAMIC_BLOCKED symbol=RIVN" not in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=RIVN" not in caplog.text
    mock_place_order.assert_called_once()
    assert mock_place_order.call_args.args[2]["notional"] == pytest.approx(300.0)


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_weak_catalyst_exception_enabled_rejects_non_qualifying_candidate(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS

    _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "LOWQ", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    signal = _live_weak_exception_signal("LOWQ")
    signal["gain_pct"] = 8.0
    signal["day_gain_pct"] = 8.0
    kwargs["signals"] = [signal]
    kwargs["config"]["broker"] = {"paper": False}
    _enable_live_weak_catalyst_exception(kwargs["config"])
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "LIVE_WEAK_CATALYST_EXCEPTION_REJECT symbol=LOWQ reason=gain_below_min" in caplog.text
    assert "WEAK_CATALYST_DYNAMIC_BLOCKED symbol=LOWQ reason=non_exceptional_zero_news_live" in caplog.text
    mock_place_order.assert_not_called()


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_weak_catalyst_exception_daily_cap_enforced(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS

    _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "buy", "symbol": "RIVN", "notional": 1200.0},
            {"action": "buy", "symbol": "ARCT", "notional": 1200.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="exp")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [_live_weak_exception_signal("RIVN"), _live_weak_exception_signal("ARCT")]
    kwargs["config"]["broker"] = {"paper": False}
    _enable_live_weak_catalyst_exception(kwargs["config"])
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "LIVE_WEAK_CATALYST_EXCEPTION_ALLOW symbol=RIVN" in caplog.text
    assert "LIVE_WEAK_CATALYST_EXCEPTION_REJECT symbol=ARCT reason=daily_max_positions_per_day" in caplog.text
    assert mock_place_order.call_count == 1


def test_live_weak_catalyst_exception_never_allows_options_or_trend() -> None:
    from src.capital_allocator_loop import _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS

    _LIVE_WEAK_CATALYST_EXCEPTION_DAY_COUNTS.clear()
    config = {"dynamic_universe": {}}
    _enable_live_weak_catalyst_exception(config)
    option_signal = _live_weak_exception_signal("AAPL260717C00150000")
    ok, reason, _meta = _allocator_live_weak_catalyst_exception_experiment_decision(
        option_signal,
        config=config,
        user_id="live_bot",
        symbol="AAPL260717C00150000",
        side="buy",
        price=10.0,
        spread_pct=0.2,
        notional=1200.0,
        current_positions={},
        tracked={},
        positions=[],
        dt=datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
    )
    assert ok is False
    assert reason == "option_symbol"

    trend_signal = _live_weak_exception_signal("RIVN")
    trend_signal.update({"route": "trend_long", "source": "scoring", "dynamic_candidate": False})
    ok, reason, _meta = _allocator_live_weak_catalyst_exception_experiment_decision(
        trend_signal,
        config=config,
        user_id="live_bot",
        symbol="RIVN",
        side="buy",
        price=10.0,
        spread_pct=0.2,
        notional=1200.0,
        current_positions={},
        tracked={},
        positions=[],
        dt=datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc),
    )
    assert ok is False
    assert reason == "not_dynamic_stock_route"


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_weak_catalyst_execution_cooldown_skips_repeated_dispatch(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL

    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "CORD", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("CORD")
    signal.update({"relative_volume": 0.70, "scanner_score": 55.0, "gain_pct": 2.5})
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [signal]
    kwargs["config"]["broker"] = {"paper": False}
    kwargs["config"]["dynamic_universe"] = {
        "min_relative_volume": 0.3,
        "min_price": 2.0,
        "weak_catalyst_execution_cooldown_minutes": 10,
    }
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)
        kwargs["dt"] = datetime(2026, 6, 11, 10, 5, tzinfo=timezone.utc)
        execute_capital_allocator_pass(**kwargs)

    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=CORD reason=weak_catalyst_dynamic_non_exceptional_live minutes=10" in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_ACTIVE symbol=CORD reason=weak_catalyst_dynamic_non_exceptional_live" in caplog.text
    assert "ORDER_SKIP symbol=CORD reason=weak_catalyst_execution_cooldown source=capital_allocator" in caplog.text
    assert "ALLOCATOR_DISPATCH_SKIPPED symbol=CORD reason=weak_catalyst_execution_cooldown" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_weak_catalyst_execution_cooldown_expires(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL

    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "FBL", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("FBL")
    signal.update({"relative_volume": 0.70, "scanner_score": 55.0, "gain_pct": 2.5})
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [signal]
    kwargs["config"]["broker"] = {"paper": False}
    kwargs["config"]["dynamic_universe"] = {
        "min_relative_volume": 0.3,
        "min_price": 2.0,
        "weak_catalyst_execution_cooldown_minutes": 10,
    }
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)
        kwargs["dt"] = datetime(2026, 6, 11, 10, 11, tzinfo=timezone.utc)
        execute_capital_allocator_pass(**kwargs)

    assert "DYNAMIC_EXECUTION_COOLDOWN_EXPIRED symbol=FBL reason=elapsed" in caplog.text
    assert caplog.text.count("WEAK_CATALYST_DYNAMIC_BLOCKED symbol=FBL reason=non_exceptional_zero_news_live") == 2


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_weak_catalyst_execution_cooldown_does_not_apply_to_paper(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL

    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "CORD", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [_scanner_selected_dynamic_signal("CORD")]
    kwargs["config"]["broker"] = {"paper": True}
    kwargs["config"]["dynamic_universe"] = {
        "min_relative_volume": 0.3,
        "min_price": 2.0,
        "weak_catalyst_execution_cooldown_minutes": 10,
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=CORD" not in caplog.text
    assert _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL == {}


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_weak_catalyst_execution_cooldown_does_not_block_trend_long(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL

    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "CORD", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    weak_kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    weak_kwargs["user_id"] = "live_bot"
    weak_kwargs["signals"] = [_scanner_selected_dynamic_signal("CORD")]
    weak_kwargs["config"]["broker"] = {"paper": False}
    weak_kwargs["config"]["dynamic_universe"] = {
        "min_relative_volume": 0.3,
        "min_price": 2.0,
        "weak_catalyst_execution_cooldown_minutes": 10,
    }
    weak_kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    weak_kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    trend_signal = {
        "sym_u": "CORD",
        "symbol": "CORD",
        "score": 2.0,
        "strength_eff": 2.0,
        "source": "scoring",
        "route": "trend_long",
        "dynamic_candidate": False,
        "news_score": 0.0,
        "event_score": 0.0,
        "catalyst_score": 0.0,
    }
    trend_kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    trend_kwargs["user_id"] = "live_bot"
    trend_kwargs["signals"] = [trend_signal]
    trend_kwargs["config"]["broker"] = {"paper": False}
    trend_kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)
    mock_place_order.return_value = MagicMock(id="trend-cord")

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**weak_kwargs)
        execute_capital_allocator_pass(**trend_kwargs)

    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=CORD" in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_ACTIVE symbol=CORD" not in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_EXPIRED symbol=CORD reason=no_longer_weak_catalyst_dynamic" in caplog.text
    mock_place_order.assert_called_once()


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_weak_catalyst_execution_cooldown_clears_for_strong_catalyst(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL

    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "CORD", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    weak_kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    weak_kwargs["user_id"] = "live_bot"
    weak_kwargs["signals"] = [_scanner_selected_dynamic_signal("CORD")]
    weak_kwargs["config"]["broker"] = {"paper": False}
    weak_kwargs["config"]["dynamic_universe"] = {
        "min_relative_volume": 0.3,
        "min_price": 2.0,
        "weak_catalyst_execution_cooldown_minutes": 10,
    }
    weak_kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    weak_kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    strong_signal = _scanner_selected_dynamic_signal("CORD")
    strong_signal.update({"news_score": 8.0, "event_score": 8.0, "catalyst_score": 0.8})
    strong_kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    strong_kwargs["user_id"] = "live_bot"
    strong_kwargs["signals"] = [strong_signal]
    strong_kwargs["config"]["broker"] = {"paper": False}
    strong_kwargs["config"]["dynamic_universe"] = {"min_relative_volume": 0.3, "min_price": 2.0}
    strong_kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)
    mock_place_order.return_value = MagicMock(id="strong-cord")

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**weak_kwargs)
        execute_capital_allocator_pass(**strong_kwargs)

    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=CORD" in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_EXPIRED symbol=CORD reason=strong_catalyst" in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_ACTIVE symbol=CORD" not in caplog.text
    mock_place_order.assert_called_once()


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_weak_catalyst_execution_cooldown_does_not_block_sells(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL

    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "CORD", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    weak_kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    weak_kwargs["user_id"] = "live_bot"
    weak_kwargs["signals"] = [_scanner_selected_dynamic_signal("CORD")]
    weak_kwargs["config"]["broker"] = {"paper": False}
    weak_kwargs["config"]["dynamic_universe"] = {
        "min_relative_volume": 0.3,
        "min_price": 2.0,
        "weak_catalyst_execution_cooldown_minutes": 10,
    }
    weak_kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    weak_kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    sell_inst = MagicMock()
    sell_inst.last_skipped_symbols = set()
    sell_inst.allocate = MagicMock(return_value=[{"action": "sell", "symbol": "CORD", "notional": 1000.0}])
    sell_kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    sell_kwargs["user_id"] = "live_bot"
    sell_kwargs["signals"] = [_scanner_selected_dynamic_signal("CORD")]
    sell_kwargs["config"]["broker"] = {"paper": False}
    sell_kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)
    mock_place_order.return_value = MagicMock(id="sell-cord")

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**weak_kwargs)
        mock_allocator_cls.return_value = sell_inst
        execute_capital_allocator_pass(**sell_kwargs)

    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=CORD" in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_ACTIVE symbol=CORD" not in caplog.text
    mock_place_order.assert_called_once()
    assert mock_place_order.call_args.args[2]["action"] == "sell"


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_weak_catalyst_dynamic_exceptional_is_reduced(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import (
        _ALLOCATOR_SYMBOL_BLOCK_UNTIL,
        _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL,
    )

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    _DYNAMIC_WEAK_CATALYST_EXECUTION_COOLDOWN_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "MU", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("MU")
    signal.update(
        {
            "relative_volume": 1.70,
            "scanner_score": 85.0,
            "gain_pct": 5.0,
            "price_above_vwap": True,
            "news_score": 0.0,
            "event_score": 0.0,
            "catalyst_score": 0.0,
        }
    )
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [signal]
    kwargs["config"]["broker"] = {"paper": False}
    kwargs["config"]["dynamic_universe"] = {"min_relative_volume": 0.3, "min_price": 2.0}
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
        "live_weak_catalyst_guard": {"enabled": True},
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "WEAK_CATALYST_DYNAMIC_REDUCED symbol=MU reason=exceptional_zero_news_live original_notional=2000.00 reduced_notional=600.00" in caplog.text
    assert "DYNAMIC_EXECUTION_COOLDOWN_START symbol=MU" not in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=MU action=buy notional=600.00" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_dynamic_aggressive_dispatch_caps_notional_and_logs_order_intent(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "FCEL",
                "notional": 2000.0,
                "source": "dynamic_aggressive",
                "route": "dynamic_aggressive_scalp",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="aggr-fcel")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [
        _aggressive_row(
            symbol="FCEL",
            sym_u="FCEL",
            route="dynamic_aggressive_scalp",
            source="dynamic_aggressive",
            notional=500.0,
        )
    ]
    kwargs["config"] = _aggressive_config()
    kwargs["config"]["options"] = {"enabled": False}
    kwargs["config"]["dynamic_universe"] = {"min_relative_volume": 0.3, "min_price": 2.0}
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)
    kwargs["broker"].get_open_orders.return_value = []

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    action = mock_place_order.call_args.args[2]
    assert action["route"] == "dynamic_aggressive_scalp"
    assert action["notional"] == pytest.approx(500.0)
    assert "DYNAMIC_AGGRESSIVE_SIZE symbol=FCEL notional=500.00 cap=500.00" in caplog.text
    assert "DYNAMIC_AGGRESSIVE_ORDER_INTENT symbol=FCEL notional=500.00" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=FCEL action=buy notional=500.00" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_strong_catalyst_dynamic_size_unchanged(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "CATX", "notional": 2000.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("CATX")
    signal.update(
        {
            "relative_volume": 0.70,
            "scanner_score": 55.0,
            "price_above_vwap": True,
            "news_score": 7.0,
            "event_score": 0.0,
            "catalyst_score": 0.0,
        }
    )
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [signal]
    kwargs["config"]["dynamic_universe"] = {"min_relative_volume": 0.3, "min_price": 2.0}
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=CATX" not in caplog.text
    assert "DYNAMIC_WEAK_CATALYST_SIZE_REDUCED symbol=CATX" not in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=CATX action=buy notional=2000.00" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_blze_scanner_approved_no_catalyst_reaches_allocator_with_alignment_marker(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "BLZE", "notional": 900.0}])
    mock_allocator_cls.return_value = inst
    signal = _scanner_selected_dynamic_signal("BLZE")
    signal.update(
        {
            "score": 0.0,
            "dynamic_score": 0.0,
            "scanner_score": 37.0,
            "price": 6.75,
            "avg_volume": 6000,
            "relative_volume": 1.35,
            "entry_eval_effective_min_rel_volume": 1.2,
            "day_gain_pct": 18.0,
            "entry_alignment_ok": True,
            "alignment_ok": True,
        }
    )
    signal.pop("price_above_vwap", None)
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [signal]
    kwargs["config"]["dynamic_universe"] = {
        "min_avg_volume": 5000,
        "min_relative_volume": 1.2,
        "min_price": 2.0,
    }
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 15.0,
        "min_relative_volume": 1.2,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=6.75, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    inst.allocate.assert_called_once()
    assert inst.allocate.call_args.kwargs["candidates"][0]["symbol"] == "BLZE"
    assert "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=BLZE reason=scanner_selected" in caplog.text
    assert "BLZE:no_catalyst" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_no_catalyst_without_scanner_selected_still_rejects(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    signal = _scanner_selected_dynamic_signal("HIVE")
    signal.pop("scanner_selected")
    kwargs["signals"] = [signal]
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert "DYNAMIC_ALLOCATOR_CATALYST_REQUIRED symbol=HIVE reason=no_catalyst" in caplog.text
    assert "ALLOCATOR_REJECT_REASON symbol=HIVE reason=no_catalyst stage=profile_filter" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_scanner_selected_dynamic_wide_spread_still_rejects(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "HIVE", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [_scanner_selected_dynamic_signal("HIVE")]
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=9.0)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    inst.allocate.assert_called_once()
    assert "DYNAMIC_ALLOCATOR_CATALYST_BYPASS symbol=HIVE reason=scanner_selected" in caplog.text
    assert "ALLOCATOR_REJECT HIVE reason=dynamic spread 9.000% >" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=HIVE" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_scanner_selected_dynamic_no_quote_still_rejects(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "HIVE", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [_scanner_selected_dynamic_signal("HIVE")]
    kwargs["broker"].get_latest_quote.return_value = None

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    inst.allocate.assert_not_called()
    assert "ALLOCATOR_SYMBOL_BLOCKED symbol=HIVE reason=no_quote" in caplog.text
    assert "ALLOCATOR_CANDIDATE_REJECT symbol=HIVE reason=hard_liquidity_gate detail=no_quote" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_buy_action_dispatches_order_for_intc(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "INTC",
                "notional": 4000.0068,
                "source": "dynamic_universe",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="paper-intc")

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**_allocator_dispatch_common_kwargs(tmp_path))

    mock_place_order.assert_called_once()
    action_spec = mock_place_order.call_args.args[2]
    assert action_spec["symbol"] == "INTC"
    assert action_spec["action"] == "buy"
    assert action_spec["source"] == "dynamic_universe"
    assert "ALLOCATOR_ACTIONS count=1 actions=[{'action': 'buy', 'symbol': 'INTC'" in caplog.text
    assert "ALLOCATOR_DISPATCH_START symbol=INTC action=buy notional=4000.00" in caplog.text
    assert "ALLOCATOR_DISPATCH_START action=buy symbol=INTC notional=4000.00" in caplog.text
    assert "ORDER_INTENT symbol=INTC side=buy notional=4000.00 source=capital_allocator" in caplog.text
    assert "ALLOCATOR_ORDER_INTENT symbol=INTC side=buy notional=4000.00 qty=" in caplog.text
    assert (
        "ENTRY_TERMINAL_OUTCOME symbol=INTC stage=allocator_order_intent "
        "reason=order_intent route=dynamic_momentum_override"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=INTC action=buy notional=4000.00 order_id=paper-intc" in caplog.text
    assert "ALLOCATOR_DISPATCH_DONE symbol=INTC result=submitted reason=submitted" in caplog.text
    assert "ALLOCATOR_DISPATCH_END symbol=INTC result=submitted reason=submitted" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_buy_action_logs_order_skip_when_dispatch_blocked(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "INTC",
                "notional": 4000.0068,
                "source": "dynamic_universe",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = None
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["engine"].execution.last_order_build_reject_reason = "risk_cap"

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ORDER_INTENT symbol=INTC side=buy notional=4000.00 source=capital_allocator" in caplog.text
    assert "ORDER_SKIP symbol=INTC reason=execution_blocked source=capital_allocator" in caplog.text
    assert "ALLOCATOR_DISPATCH_SKIPPED symbol=INTC reason=execution_blocked" in caplog.text
    assert "ALLOCATOR_DISPATCH_DONE symbol=INTC result=skipped reason=execution_blocked" in caplog.text
    assert "ALLOCATOR_DISPATCH_END symbol=INTC result=skipped reason=execution_blocked" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_entries_disabled_block_is_expected_control_flow(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "INTC",
                "notional": 4000.0068,
                "source": "dynamic_universe",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.side_effect = EntryBlocked(ENTRY_BLOCKED_MODE_ENTRIES_DISABLED)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**_allocator_dispatch_common_kwargs(tmp_path))

    mock_place_order.assert_called_once()
    assert f"ENTRY_BLOCKED_MODE symbol=INTC action=buy notional=4000.00" in caplog.text
    assert f"reason={ENTRY_BLOCKED_MODE_ENTRIES_DISABLED}" in caplog.text
    assert f"ALLOCATOR_DISPATCH_BLOCKED symbol=INTC reason={ENTRY_BLOCKED_MODE_ENTRIES_DISABLED}" in caplog.text
    assert f"ALLOCATOR_DISPATCH_END symbol=INTC result=blocked reason={ENTRY_BLOCKED_MODE_ENTRIES_DISABLED}" in caplog.text
    assert "ALLOCATOR_DISPATCH_ERROR symbol=INTC" not in caplog.text
    assert "ALLOCATOR_ACTION_EXCEPTION symbol=INTC" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_empty_actions_logs_count_zero(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["ca_cfg"]["fallback_on_empty_alloc"] = False
    kwargs["ca_cfg"]["force_minimum_trade_single_candidate"] = False
    kwargs["ca_cfg"]["allow_no_trade_cycles"] = True

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "ALLOCATOR_ACTIONS count=0 actions=[]" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_paper_dispatch_logs_order_intent_before_broker_call(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(
        return_value=[{"action": "buy", "symbol": "INTC", "notional": 4000.0068}]
    )
    mock_allocator_cls.return_value = inst

    def _place_order_side_effect(*_args, **_kwargs):
        assert "ORDER_INTENT symbol=INTC side=buy notional=4000.00 source=capital_allocator" in caplog.text
        return MagicMock(id="paper-intent")

    mock_place_order.side_effect = _place_order_side_effect

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**_allocator_dispatch_common_kwargs(tmp_path))

    mock_place_order.assert_called_once()
    assert "ALLOCATOR_DISPATCH_DONE symbol=INTC result=submitted reason=submitted" in caplog.text
    assert "ORDER_SUBMITTED symbol=INTC side=buy notional=4000.00 source=capital_allocator" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_repeated_allocator_buy_actions_dispatch_each_action(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "INTC",
                "notional": 4000.0068,
                "source": "dynamic_universe",
                "dynamic_candidate": True,
            },
            {
                "action": "buy",
                "symbol": "ORCL",
                "notional": 1200.0,
                "source": "dynamic_universe",
                "dynamic_candidate": True,
            },
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.side_effect = [MagicMock(id="paper-intc-1"), MagicMock(id="paper-intc-2")]
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"].append(
        {
            "sym_u": "ORCL",
            "symbol": "ORCL",
            "score": 1.5,
            "strength_eff": 1.5,
            "notional": 1200.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "news_score": 8.0,
            "catalyst_score": 0.8,
            "relative_volume": 2.0,
        }
    )

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert mock_place_order.call_count == 2
    assert "ALLOCATOR_ACTIONS count=2" in caplog.text
    assert "ALLOCATOR_DISPATCH_START symbol=INTC action=buy" in caplog.text
    assert "ALLOCATOR_DISPATCH_START symbol=ORCL action=buy" in caplog.text
    assert "ORDER_INTENT symbol=INTC side=buy" in caplog.text
    assert "ORDER_INTENT symbol=ORCL side=buy" in caplog.text
    assert caplog.text.count("ALLOCATOR_DISPATCH_DONE symbol=") >= 2


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_drops_buys_when_allow_allocator_buys_false(
    mock_allocator_cls: MagicMock, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "buy", "symbol": "SPY", "notional": 500.0},
            {"action": "sell", "symbol": "XLP", "notional": 500.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=False,
        )
    assert "dropping 1 buy" in caplog.text
    assert "ORDER_SKIP symbol=SPY reason=post_planner_no_recycle_drop_buys source=capital_allocator" in caplog.text
    assert "ALLOCATOR_DISPATCH_DONE symbol=SPY result=skipped reason=post_planner_no_recycle_drop_buys" in caplog.text
    out = capsys.readouterr().out
    assert "ALLOCATOR ACTIONS:" in out
    _tail = out.split("ALLOCATOR ACTIONS:", 1)[1].strip()
    assert "XLP" in _tail
    assert "SPY" not in _tail


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_no_recycle_block_drops_buys(
    mock_allocator_cls: MagicMock, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """``risk.no_recycle_above_pct`` (live) maps to *no_recycle_block*; allocator must not submit buys."""
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "buy", "symbol": "SPY", "notional": 500.0},
            {"action": "sell", "symbol": "XLP", "notional": 500.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    with caplog.at_level("WARNING", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[],
            broker=MagicMock(),
            engine=MagicMock(),
            config={
                "options": {"enabled": False},
                "risk": {"no_recycle_above_pct": 0.94},
            },
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            no_recycle_block=True,
        )
    assert "no_recycle" in caplog.text
    out = capsys.readouterr().out
    _tail = out.split("ALLOCATOR ACTIONS:", 1)[1].strip()
    assert "XLP" in _tail
    assert "SPY" not in _tail


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_risk_control_drops_buys_when_gross_over_threshold(
    mock_allocator_cls: MagicMock, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """Gross (%% of equity) above ``risk_control_gross_frac`` → mode risk_control, no allocator buys."""
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "buy", "symbol": "SPY", "notional": 500.0},
            {"action": "sell", "symbol": "XLP", "notional": 500.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    with caplog.at_level("WARNING", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "risk_control_gross_frac": 0.95,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=96.0,
        )
    assert "capital_allocator: risk_control" in caplog.text
    out = capsys.readouterr().out
    assert "ALLOCATOR mode: risk_control" in out
    _tail = out.split("ALLOCATOR ACTIONS:", 1)[1].strip()
    assert "XLP" in _tail
    assert "SPY" not in _tail


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_gross_at_threshold_stays_normal_mode(
    mock_allocator_cls: MagicMock, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """Gross *equal* to threshold (95%%) is not risk override (``> 0.95`` fraction, strict)."""
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "sell", "symbol": "XLP", "notional": 500.0},
            {"action": "buy", "symbol": "SPY", "notional": 500.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    execute_capital_allocator_pass(
        signals=[],
        broker=MagicMock(),
        engine=MagicMock(),
        config={"options": {"enabled": False}},
        dt=MagicMock(strftime=MagicMock(return_value="10:00")),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "risk_control_gross_frac": 0.95,
            # Without this, default near-cap 0.5× buy/sell trim can remove the buy (mleg) at 95%% gross.
            "net_reduction_max_buy_to_sell_ratio": 1.0,
        },
        user_id="u1",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
        allow_allocator_buys=True,
        gross_exposure_pct=95.0,
    )
    out = capsys.readouterr().out
    assert "ALLOCATOR mode: normal" in out
    assert "ALLOCATOR mode: risk_control" not in out
    assert "SPY" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_under_invested_mode_deploy(
    mock_allocator_cls: MagicMock, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """Long gross (%% of equity) below ``min_gross_deployment_pct`` → ``mode: deploy``."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    execute_capital_allocator_pass(
        signals=[],
        broker=MagicMock(),
        engine=MagicMock(),
        config={"options": {"enabled": False}},
        dt=MagicMock(strftime=MagicMock(return_value="10:00")),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "risk_control_gross_frac": 0.95,
            "min_gross_deployment_pct": 0.85,
        },
        user_id="u1",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
        allow_allocator_buys=True,
        gross_exposure_pct=80.0,
    )
    out = capsys.readouterr().out
    assert "ALLOCATOR mode: deploy" in out
    assert "ALLOCATOR mode: normal" not in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_force_allocate_bullish_skips_net_sell_gte_buy_trim(
    mock_allocator_cls: MagicMock, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """``regime_score`` 4 + gross below min deploy: buy-only plan survives (no net_sell_gte trim)."""
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "buy", "symbol": "SPY", "notional": 1000.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "min_gross_deployment_pct": 0.85,
                "bullish_force_minimum_deploy": True,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=77.0,
            regime_score=4,
        )
    out = capsys.readouterr().out
    assert "force_allocate" in caplog.text
    assert "1000" in out
    # Not the post-pass trim (distinct from the force_allocate log line text)
    assert "net_sell_gte_buy trim (buys" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_no_force_allocate_regime3_trims_buy_only_to_empty(
    mock_allocator_cls: MagicMock, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Same as bullish force, but score 3 — require_net_sell still strips a buy-only plan."""
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "buy", "symbol": "SPY", "notional": 1000.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "min_gross_deployment_pct": 0.85,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=77.0,
            regime_score=3,
        )
    out = capsys.readouterr().out
    assert "force_allocate" not in caplog.text
    assert "net_sell_gte_buy trim" in caplog.text
    assert (
        "POST_PLANNER_ACTION_TRACE stage=net_sell_gte_buy before_count=1 after_count=0 "
        "before_actions=buy:SPY:1000.00 after_actions=none removed_actions=buy:SPY:1000.00"
    ) in caplog.text
    assert "1000" not in out
    assert "[]" in out or "0" in out  # no buy notional left in printed actions


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_no_trade_reason_reports_post_planner_removal_stage(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[{"action": "buy", "symbol": "SPY", "notional": 1000.0}]
    )
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[{"sym_u": "SPY", "composite_score": 9.0}],
            broker=MagicMock(),
            engine=MagicMock(),
            config={
                "options": {"enabled": False},
                "_replay_mode": "offline_replay",
                "_broker_mock": True,
                "_market_open": "replay_not_evaluated",
            },
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": False,
                "allow_no_trade_cycles": True,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state={"daily_loss_lockout": False},
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    assert "no trade cycle allowed" in capsys.readouterr().out
    assert "ALLOCATOR_ACTION_CREATED symbol=SPY action=buy notional=1000.00" in caplog.text
    assert (
        "POST_PLANNER_ACTION_TRACE stage=net_sell_gte_buy before_count=1 after_count=0"
    ) in caplog.text
    assert (
        "ALLOCATOR_SKIP_REASON symbol=SPY reason=actions_removed_by_post_planner_filter "
        "last_removal_stage=net_sell_gte_buy"
    ) in caplog.text
    assert (
        "TRADE_CYCLE_GATE symbol=SPY replay_mode=offline_replay broker_mock=True "
        "market_open=replay_not_evaluated trade_cycle_allowed=True allow_buys=True"
    ) in caplog.text
    assert (
        "skip_reason=actions_removed_by_post_planner_filter last_removal_stage=net_sell_gte_buy"
    ) in caplog.text
    assert (
        "ENTRY_PIPELINE_STAGE symbol=SPY stage=post_planner result=skipped "
        "reason=actions_removed_by_post_planner_filter last_removal_stage=net_sell_gte_buy"
    ) in caplog.text
    assert (
        "OPTION_PIPELINE_STAGE symbol=SPY stage=post_planner result=skipped "
        "reason=entry_pipeline_not_reached:actions_removed_by_post_planner_filter "
        "last_removal_stage=net_sell_gte_buy"
    ) in caplog.text
    assert "ALLOCATOR_SKIP_REASON symbol=SPY reason=no_trade_cycle_allowed" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_min_gross_deployment_zero_disables_deploy_mode(
    mock_allocator_cls: MagicMock, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """``min_gross_deployment_pct: 0`` → never ``deploy`` (use ``normal`` when not risk)."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    execute_capital_allocator_pass(
        signals=[],
        broker=MagicMock(),
        engine=MagicMock(),
        config={"options": {"enabled": False}},
        dt=MagicMock(strftime=MagicMock(return_value="10:00")),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "min_gross_deployment_pct": 0.0,
        },
        user_id="u1",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
        allow_allocator_buys=True,
        gross_exposure_pct=30.0,
    )
    out = capsys.readouterr().out
    assert "ALLOCATOR mode: normal" in out
    assert "ALLOCATOR mode: deploy" not in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_near_cap_trims_buys_to_max_buy_to_sell_ratio(
    mock_allocator_cls: MagicMock, capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """90%% gross vs 0.92 meff and 0.9 *rel* is “near cap”; 0.5 *ratio* ⇒ buys ≤ half of sells (800→500)."""
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "sell", "symbol": "XLP", "notional": 1000.0},
            {"action": "buy", "symbol": "SPY", "notional": 800.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    config = {
        "options": {"enabled": False},
        "portfolio": {
            "exposure_gates": {
                "enabled": True,
                "max_total_exposure_frac": 0.92,
            }
        },
    }
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[],
            broker=MagicMock(),
            engine=MagicMock(),
            config=config,
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 100.0,
                "rotate_trim_fraction": 0.3,
                "risk_control_gross_frac": 0.95,
                "net_reduction_max_buy_to_sell_ratio": 0.5,
                "net_reduction_near_cap_relative_to_max": 0.9,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=90.0,
        )
    out = capsys.readouterr().out
    assert "ALLOCATOR mode: normal" in out
    assert "'notional': 500.0" in out
    assert "near_cap net reduction" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_skips_action_below_min_realloc_notional(
    mock_allocator_cls: MagicMock, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Sub-``min_realloc_leg`` clips are not sent to the broker (no quote / order)."""
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[{"action": "buy", "symbol": "SPY", "notional": 50.0}]
    )
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_latest_quote = MagicMock()
    engine = MagicMock()
    config = {"options": {"enabled": False}}
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[{"sym_u": "SPY", "strength_eff": 0.9}],
            broker=broker,
            engine=engine,
            config=config,
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=["SPY"],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                # Net trim would clear this buy-only $50 leg before the submit-time min-clip check.
                "require_net_sell_gte_buy": False,
                # Disable pre-submit netting so the per-action min_leg skip log is exercised.
                "consolidate_net_before_submit": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            exit_context=None,
        )
    broker.get_latest_quote.assert_not_called()
    assert "min_realloc_leg" in caplog.text
    assert "300" in caplog.text


def test_empty_alloc_equal_split_top_two_by_score() -> None:
    out = empty_alloc_equal_split_buys(
        candidates=[
            {"symbol": "A", "score": 1.0},
            {"symbol": "B", "score": 9.0},
        ],
        cash=10_000.0,
        min_realloc_leg=300.0,
        top_n=5,
    )
    assert len(out) == 2
    by_sym = {x["symbol"]: x["notional"] for x in out}
    assert by_sym["B"] == pytest.approx(5000.0)
    assert by_sym["A"] == pytest.approx(5000.0)


def test_empty_alloc_equal_split_takes_top_five() -> None:
    cands = [{"symbol": f"S{i}", "score": float(i)} for i in range(10)]
    out = empty_alloc_equal_split_buys(
        candidates=cands,
        cash=50_000.0,
        min_realloc_leg=1.0,
        top_n=5,
    )
    assert len(out) == 5
    want = ["S9", "S8", "S7", "S6", "S5"]
    assert [x["symbol"] for x in out] == want
    for x in out:
        assert x["notional"] == pytest.approx(10_000.0)


def test_empty_alloc_equal_split_empty_when_per_leg_below_min() -> None:
    assert (
        empty_alloc_equal_split_buys(
            candidates=[{"symbol": "A", "score": 1.0}, {"symbol": "B", "score": 2.0}],
            cash=500.0,
            min_realloc_leg=300.0,
            top_n=5,
        )
        == []
    )


def test_empty_alloc_fixed_size_buys_uses_equity_size_pct_and_cash_cap() -> None:
    out = empty_alloc_fixed_size_buys(
        candidates=[
            {"symbol": "A", "score": 1.0},
            {"symbol": "B", "score": 9.0},
            {"symbol": "C", "score": 5.0},
        ],
        equity=100_000.0,
        cash=25_000.0,
        min_realloc_leg=300.0,
        top_n=2,
        size_pct=0.1,
    )
    assert [x["symbol"] for x in out] == ["B", "C"]
    assert [x["notional"] for x in out] == [pytest.approx(10_000.0), pytest.approx(10_000.0)]


def test_select_top_candidates_with_group_cap_limits_same_bucket() -> None:
    groups = _normalized_correlation_groups(
        {"tech": ["AAPL", "MSFT", "NVDA"], "indices": ["SPY", "IWM", "DIA"]}
    )
    selected, skipped = select_top_candidates_with_group_cap(
        [
            {"symbol": "NVDA", "score": 9.0},
            {"symbol": "MSFT", "score": 8.0},
            {"symbol": "AAPL", "score": 7.0},
            {"symbol": "SPY", "score": 6.0},
            {"symbol": "IWM", "score": 5.0},
        ],
        top_n=4,
        max_per_group=2,
        correlation_groups=groups,
    )
    assert [x["symbol"] for x in selected] == ["NVDA", "MSFT", "SPY", "IWM"]
    assert "tech:AAPL" in skipped


def test_select_top_candidates_skips_held_symbols_at_hard_cap() -> None:
    selected, skipped = select_top_candidates_with_group_cap(
        [
            {"symbol": "AAPL", "score": 9.0},
            {"symbol": "SMH", "score": 8.0},
            {"symbol": "AMZN", "score": 7.0},
        ],
        top_n=2,
        portfolio=[
            {"symbol": "AAPL", "value": 3_100.0},
            {"symbol": "SMH", "value": 3_105.0},
        ],
        equity=10_000.0,
        default_hard_cap_frac=0.262,
    )
    assert [x["symbol"] for x in selected] == ["AMZN"]
    assert skipped == ["cap:AAPL", "cap:SMH"]


@patch("src.capital_allocator_loop.apply_cooldown", side_effect=lambda actions, portfolio, exit_context=None: actions)
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_allocator_idle_fallback_after_two_empty_cycles(
    mock_allocator_cls: MagicMock,
    _mock_cooldown: MagicMock,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    _ALLOCATOR_EMPTY_ACTION_CYCLES.clear()
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    common = dict(
        signals=[
            {"sym_u": "A", "composite_score": 1.0, "notional": 2000.0},
            {"sym_u": "B", "composite_score": 8.0, "notional": 1200.0},
        ],
        broker=MagicMock(),
        engine=MagicMock(),
        config={"options": {"enabled": False}},
        dt=MagicMock(strftime=MagicMock(return_value="10:00")),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "require_net_sell_gte_buy": True,
            "if_no_actions_cycles": 2,
            "fallback_pick_top_n": 2,
            "fallback_size_pct": 0.1,
            "fallback_on_empty_alloc": False,
        },
        user_id="u-idle",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
        exit_context=None,
    )
    execute_capital_allocator_pass(**common)
    out1 = capsys.readouterr().out
    assert "FORCING MINIMUM TRADE" not in out1
    assert "ALLOCATOR ACTIONS:" in out1
    assert "selected_must_execute_force_buy" not in out1
    execute_capital_allocator_pass(**common)
    out2 = capsys.readouterr().out
    assert "ALLOCATOR ACTIONS:" in out2
    assert "B" in out2
    assert "idle_fallback" in out2


@patch("src.capital_allocator_loop.apply_cooldown", side_effect=lambda actions, portfolio, exit_context=None: actions)
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_allocator_idle_fallback_skips_at_gross_cap(
    mock_allocator_cls: MagicMock,
    _mock_cooldown: MagicMock,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    _ALLOCATOR_EMPTY_ACTION_CYCLES.clear()
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    execute_capital_allocator_pass(
        signals=[
            {"sym_u": "A", "composite_score": 1.0, "notional": 2000.0},
            {"sym_u": "B", "composite_score": 8.0, "notional": 1200.0},
        ],
        broker=MagicMock(),
        engine=MagicMock(),
        config={"options": {"enabled": False}},
        dt=MagicMock(strftime=MagicMock(return_value="10:00")),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "require_net_sell_gte_buy": True,
            "if_no_actions_cycles": 1,
            "fallback_pick_top_n": 2,
            "fallback_size_pct": 0.1,
            "idle_fallback_max_gross_pct": 0.85,
            "fallback_on_empty_alloc": False,
            "force_minimum_trade_single_candidate": False,
        },
        user_id="u-idle-cap",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
        exit_context=None,
        gross_exposure_pct=85.0,
    )
    out = capsys.readouterr().out
    assert "idle fallback skipped — gross exposure too high" in out
    assert "reason skipped: idle_fallback_gross_cap" in out
    assert "ALLOCATOR ACTIONS: []" in out


@patch("src.capital_allocator_loop.apply_cooldown", side_effect=lambda actions, portfolio, exit_context=None: actions)
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_allocator_idle_fallback_clips_to_gross_cap(
    mock_allocator_cls: MagicMock,
    _mock_cooldown: MagicMock,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    _ALLOCATOR_EMPTY_ACTION_CYCLES.clear()
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    execute_capital_allocator_pass(
        signals=[
            {"sym_u": "A", "composite_score": 1.0, "notional": 2000.0},
            {"sym_u": "B", "composite_score": 8.0, "notional": 1200.0},
        ],
        broker=MagicMock(),
        engine=MagicMock(),
        config={"options": {"enabled": False}},
        dt=MagicMock(strftime=MagicMock(return_value="10:00")),
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "require_net_sell_gte_buy": True,
            "if_no_actions_cycles": 1,
            "fallback_pick_top_n": 2,
            "fallback_size_pct": 0.1,
            "idle_fallback_max_gross_pct": 0.85,
            "fallback_on_empty_alloc": False,
            "force_minimum_trade_single_candidate": False,
        },
        user_id="u-idle-clip",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2024-01-02",
        cycle_risk_state=None,
        verbose=False,
        exit_context=None,
        gross_exposure_pct=84.0,
    )
    out = capsys.readouterr().out
    assert "ALLOCATOR ACTIONS:" in out
    assert "'notional': 500.0" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_empty_alloc_equal_split_buys(
    mock_allocator_cls: MagicMock, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """``allocate`` → [] still yields equal-split BUYs for top names by ``score`` when config allows it."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    with caplog.at_level("WARNING", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "A", "composite_score": 1.0, "notional": 2000.0},
                {"sym_u": "B", "composite_score": 8.0, "notional": 1200.0},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": True,
                "empty_alloc_top_n": 5,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
        )
    out = capsys.readouterr().out
    assert "B" in out
    assert "A" in out
    assert "7500" in out
    assert "equal-split BUY" in caplog.text
    inst.allocate.assert_called_once()


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_etf_equal_split_fallback_blocked_by_default(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "IWM", "composite_score": 8.0, "notional": 1200.0},
                {"sym_u": "QQQ", "composite_score": 7.0, "notional": 1200.0},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": True,
                "empty_alloc_top_n": 5,
                "etf_fallback_enabled": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
        )

    out = capsys.readouterr().out
    assert "ETF_FALLBACK_BLOCKED reason=config_disabled" in caplog.text
    assert "equal-split BUY" not in caplog.text
    assert "ALLOCATOR ACTIONS: []" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_etf_equal_split_fallback_blocked_when_news_candidates_present(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "IWM", "composite_score": 8.0, "notional": 1200.0},
                {"sym_u": "QQQ", "composite_score": 7.0, "notional": 1200.0},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": True,
                "empty_alloc_top_n": 5,
                "etf_fallback_enabled": True,
                "etf_fallback_only_when_no_news_candidates": True,
                "news_candidates_present": True,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
        )

    out = capsys.readouterr().out
    assert "ETF_FALLBACK_BLOCKED reason=news_candidates_present" in caplog.text
    assert "equal-split BUY" not in caplog.text
    assert "ALLOCATOR ACTIONS: []" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_etf_equal_split_fallback_allowed_when_explicitly_enabled(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "IWM", "composite_score": 8.0, "notional": 1200.0},
                {"sym_u": "QQQ", "composite_score": 7.0, "notional": 1200.0},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": True,
                "empty_alloc_top_n": 5,
                "etf_fallback_enabled": True,
                "etf_fallback_only_when_no_news_candidates": True,
                "news_candidates_present": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
        )

    out = capsys.readouterr().out
    assert "ETF_FALLBACK_BLOCKED" not in caplog.text
    assert "equal-split BUY" in caplog.text
    assert "IWM" in out
    assert "QQQ" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_etf_equal_split_fallback_respects_max_notional_pct(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "IWM", "composite_score": 8.0, "notional": 1200.0},
                {"sym_u": "QQQ", "composite_score": 7.0, "notional": 1200.0},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": True,
                "empty_alloc_top_n": 5,
                "etf_fallback_enabled": True,
                "etf_fallback_max_notional_pct": 1,
                "etf_fallback_only_when_no_news_candidates": True,
                "news_candidates_present": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
        )

    out = capsys.readouterr().out
    assert "ETF_FALLBACK_BLOCKED" not in caplog.text
    assert "equal-split BUY 2 name(s) $1000 total" in caplog.text
    assert "'notional': 500.0" in out


def test_equal_split_fallback_excludes_allocator_minimum_cash_skips(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """Fallback must not revive names the allocator skipped for minimum cash deployment."""
    broker = MagicMock()
    broker.get_latest_quote = MagicMock(return_value=None)
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "MSFT", "composite_score": 8.0, "notional": 1200.0},
                {"sym_u": "SPY", "composite_score": 7.0, "notional": 1200.0},
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": True,
                "empty_alloc_top_n": 5,
                "minimum_cash_to_deploy_pct": 0.05,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
        )

    out = capsys.readouterr().out
    assert "trade_size $500.00 + buffer $5 < minimum_cash_to_deploy 5000" in out
    assert "equal-split BUY" not in caplog.text
    assert "equal-split fallback excluding allocator-skipped symbols ['MSFT', 'SPY']" in caplog.text
    assert "ALLOCATOR ACTIONS: []" in out


@patch("src.capital_allocator_loop.apply_cooldown", side_effect=lambda actions, portfolio, exit_context=None: actions)
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_sell_skips_open_orders_and_held_for_orders(
    mock_allocator_cls: MagicMock,
    _mock_cooldown: MagicMock,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "sell", "symbol": "SPY", "notional": 1000.0},
            {"action": "buy", "symbol": "AAPL", "notional": 1000.0},
        ]
    )
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_open_orders.return_value = [{"symbol": "SPY", "side": "buy", "qty": 1}]

    execute_capital_allocator_pass(
        signals=[{"sym_u": "AAPL", "composite_score": 9.0}],
        broker=broker,
        engine=MagicMock(),
        config={"options": {"enabled": False}},
        dt=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        positions=[{"symbol": "SPY", "market_value": 5000.0, "qty": 10, "qty_held_for_orders": 2}],
        tracked={},
        current_positions={},
        eligible_active=["SPY"],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "require_net_sell_gte_buy": True,
            "fallback_on_empty_alloc": False,
            "force_minimum_trade_single_candidate": False,
        },
        user_id="u1",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2026-06-01",
        cycle_risk_state=None,
        verbose=False,
    )

    out = capsys.readouterr().out
    assert "SKIP SPY: reason=open order detail=open order exists" in out
    assert "ALLOCATOR ACTIONS: []" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_buy_skips_post_sell_rebuy_cooldown(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    record_sell_after_exit(
        "AAPL",
        "u1",
        tmp_path,
        now,
        "allocator_trim",
        5,
        {"position_states": {"enabled": True, "cooldown_after_sell_minutes": 30}},
    )
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "AAPL", "notional": 1000.0}])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_open_orders.return_value = []

    execute_capital_allocator_pass(
        signals=[{"sym_u": "AAPL", "composite_score": 9.0}],
        broker=broker,
        engine=MagicMock(),
        config={
            "options": {"enabled": False},
            "position_states": {"enabled": True, "cooldown_after_sell_minutes": 30},
        },
        dt=now,
        positions=[],
        tracked={},
        current_positions={},
        eligible_active=[],
        account_equity=100_000.0,
        cash=25_000.0,
        ca_cfg={
            "max_positions": 5,
            "symbol_cap": 0.25,
            "min_trade_size": 500.0,
            "min_realloc_leg": 300.0,
            "rotate_trim_fraction": 0.3,
            "require_net_sell_gte_buy": False,
            "fallback_on_empty_alloc": False,
            "force_minimum_trade_single_candidate": False,
        },
        user_id="u1",
        data_dir=tmp_path,
        stale_quote_max_age=60.0,
        strength_jitter_max=0.0,
        et_date_iso="2026-06-01",
        cycle_risk_state=None,
        verbose=False,
    )

    out = capsys.readouterr().out
    assert "SKIP AAPL: reason=cooldown detail=position state TRIMMED rebuy cooldown" in out
    broker.get_latest_quote.assert_not_called()


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_add_on_once_per_day_unless_signal_score_85(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    save_tracked(
        {
            "AAPL": {
                "qty": 5,
                "entry_price": 100.0,
                "adds_et_date": "2026-06-01",
                "adds_et_date_count": 1,
            }
        },
        "u1",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_open_orders.return_value = []

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[{"sym_u": "AAPL", "signal_score": 84.0}],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            positions=[{"symbol": "AAPL", "market_value": 500.0, "qty": 5}],
            tracked={},
            current_positions={},
            eligible_active=["AAPL"],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": False,
                "fallback_on_empty_alloc": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2026-06-01",
            cycle_risk_state=None,
            verbose=False,
        )

    out = capsys.readouterr().out
    assert "allocator add-on already used today; signal_score 84.0 < 85" not in out
    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert "ALLOCATOR_COOLDOWN_STATE symbol=AAPL" in caplog.text
    assert "add_on_used_today=True" in caplog.text
    assert "signal_score=84.0" in caplog.text
    assert "cooldown_expiry=after_et_date:2026-06-01" in caplog.text
    assert "ALLOCATOR_FILTER_REJECT symbol=AAPL reason=allocator_add_on_once_per_day" in caplog.text
    assert "ALLOCATOR_ACTION_CREATED symbol=AAPL" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=AAPL reason=allocator_add_on_once_per_day" not in caplog.text
    assert "ALLOCATOR_ACTION_SUBMIT_ATTEMPT symbol=AAPL" not in caplog.text
    broker.get_latest_quote.assert_not_called()


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_scanner_selected_dynamic_add_on_once_per_day_still_rejects(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    save_tracked(
        {
            "HIVE": {
                "qty": 5,
                "entry_price": 10.0,
                "adds_et_date": "2026-06-11",
                "adds_et_date_count": 1,
                "entry_source": "dynamic_universe",
            }
        },
        "paper_bot",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_open_orders.return_value = []
    broker.get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.2)
    broker.get_positions.return_value = [{"symbol": "HIVE", "market_value": 500.0, "qty": 5}]
    signal = _scanner_selected_dynamic_signal("HIVE")
    signal["signal_score"] = 84.0

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[signal],
            broker=broker,
            engine=MagicMock(),
            config={
                "options": {"enabled": False},
                "dynamic_momentum_entry": {
                    "allocator_allow_no_catalyst_if_scanner_selected": True,
                    "min_day_gain_pct": 2.0,
                    "min_relative_volume": 0.3,
                },
            },
            dt=datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
            positions=[{"symbol": "HIVE", "market_value": 500.0, "qty": 5}],
            tracked={},
            current_positions={},
            eligible_active=["HIVE"],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": False,
                "fallback_on_empty_alloc": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="paper_bot",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2026-06-11",
            cycle_risk_state=None,
            verbose=False,
        )

    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert "ALLOCATOR_FILTER_REJECT symbol=HIVE reason=allocator_add_on_once_per_day" in caplog.text
    assert "ALLOCATOR_ACTION_CREATED symbol=HIVE" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_add_on_allowed_after_45_minutes_with_followthrough(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    now = datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)
    save_tracked(
        {
            "HIVE": {
                "qty": 5,
                "entry_price": 10.0,
                "last_entry_price": 10.0,
                "last_add_time": (now - timedelta(minutes=46)).isoformat(),
                "adds_et_date": "2026-06-11",
                "adds_et_date_count": 1,
                "entry_source": "dynamic_momentum_override",
            }
        },
        "paper_bot",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    signal = _scanner_selected_dynamic_signal("HIVE")
    signal["signal_score"] = 84.0
    signal["price"] = 10.5
    signal["price_above_vwap"] = False
    kwargs["signals"] = [signal]
    kwargs["dt"] = now
    kwargs["positions"] = [{"symbol": "HIVE", "market_value": 500.0, "qty": 5}]
    kwargs["eligible_active"] = ["HIVE"]
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.5, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert inst.allocate.call_args.kwargs["candidates"][0]["symbol"] == "HIVE"
    assert "DYNAMIC_ADDON_ALLOWED symbol=HIVE reason=dynamic_followthrough" in caplog.text
    assert "ALLOCATOR_FILTER_REJECT symbol=HIVE reason=allocator_add_on_once_per_day" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_add_on_allowed_cooldown_still_blocks_no_quote(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    now = datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)
    save_tracked(
        {
            "HIVE": {
                "qty": 5,
                "entry_price": 10.0,
                "last_entry_price": 10.0,
                "last_add_time": (now - timedelta(minutes=46)).isoformat(),
                "adds_et_date": "2026-06-11",
                "adds_et_date_count": 1,
            }
        },
        "paper_bot",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "HIVE", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    signal = _scanner_selected_dynamic_signal("HIVE")
    signal["signal_score"] = 84.0
    signal["price"] = 10.5
    kwargs["signals"] = [signal]
    kwargs["dt"] = now
    kwargs["positions"] = [{"symbol": "HIVE", "market_value": 500.0, "qty": 5}]
    kwargs["eligible_active"] = ["HIVE"]
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = None

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "ALLOCATOR_CANDIDATE_REJECT symbol=HIVE reason=hard_liquidity_gate detail=no_quote" in caplog.text
    assert "DYNAMIC_ADDON_ALLOWED symbol=HIVE reason=dynamic_followthrough" not in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=HIVE" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_add_on_allowed_cooldown_still_blocks_symbol_exposure_cap(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    now = datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)
    save_tracked(
        {
            "HIVE": {
                "qty": 5,
                "entry_price": 10.0,
                "last_entry_price": 10.0,
                "last_add_time": (now - timedelta(minutes=46)).isoformat(),
                "adds_et_date": "2026-06-11",
                "adds_et_date_count": 1,
            }
        },
        "paper_bot",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    signal = _scanner_selected_dynamic_signal("HIVE")
    signal["signal_score"] = 84.0
    signal["price"] = 10.5
    kwargs["signals"] = [signal]
    kwargs["dt"] = now
    kwargs["positions"] = [{"symbol": "HIVE", "market_value": 500.0, "qty": 5}]
    kwargs["eligible_active"] = ["HIVE"]
    kwargs["ca_cfg"] = {**kwargs["ca_cfg"], "symbol_cap": 0.005}
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.5, spread_pct=0.2)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "DYNAMIC_ADDON_ALLOWED symbol=HIVE reason=dynamic_followthrough" in caplog.text
    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert "ALLOCATOR_REJECT_REASON symbol=HIVE reason=hard_cap_reached stage=deploy_selection" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_add_on_allowed_cooldown_still_blocks_wide_spread(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from src.capital_allocator_loop import _ALLOCATOR_SYMBOL_BLOCK_UNTIL

    _ALLOCATOR_SYMBOL_BLOCK_UNTIL.clear()
    now = datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)
    save_tracked(
        {
            "HIVE": {
                "qty": 5,
                "entry_price": 10.0,
                "last_entry_price": 10.0,
                "last_add_time": (now - timedelta(minutes=46)).isoformat(),
                "adds_et_date": "2026-06-11",
                "adds_et_date_count": 1,
            }
        },
        "paper_bot",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "HIVE", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    signal = _scanner_selected_dynamic_signal("HIVE")
    signal["signal_score"] = 84.0
    signal["price"] = 10.5
    kwargs["signals"] = [signal]
    kwargs["dt"] = now
    kwargs["positions"] = [{"symbol": "HIVE", "market_value": 500.0, "qty": 5}]
    kwargs["eligible_active"] = ["HIVE"]
    kwargs["config"]["dynamic_momentum_entry"] = {
        "allocator_allow_no_catalyst_if_scanner_selected": True,
        "min_day_gain_pct": 2.0,
        "min_relative_volume": 0.3,
    }
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=10.5, spread_pct=9.0)

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert "DYNAMIC_ADDON_ALLOWED symbol=HIVE reason=dynamic_followthrough" in caplog.text
    assert "ALLOCATOR_REJECT HIVE reason=dynamic spread 9.000% >" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=HIVE" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_trend_long_add_on_once_per_day_behavior_unchanged(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 6, 11, 15, 0, tzinfo=timezone.utc)
    save_tracked(
        {
            "AAPL": {
                "qty": 5,
                "entry_price": 100.0,
                "last_entry_price": 100.0,
                "last_add_time": (now - timedelta(minutes=60)).isoformat(),
                "adds_et_date": "2026-06-11",
                "adds_et_date_count": 1,
            }
        },
        "paper_bot",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.last_skipped_symbols = set()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [{"sym_u": "AAPL", "symbol": "AAPL", "signal_score": 84.0, "price": 105.0}]
    kwargs["dt"] = now
    kwargs["positions"] = [{"symbol": "AAPL", "market_value": 500.0, "qty": 5}]
    kwargs["eligible_active"] = ["AAPL"]

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert inst.allocate.call_args.kwargs["candidates"] == []
    assert "ALLOCATOR_FILTER_REJECT symbol=AAPL reason=allocator_add_on_once_per_day" in caplog.text
    assert "DYNAMIC_ADDON_ALLOWED symbol=AAPL" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_core_rebuild_allocator_action_reaches_execution(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    save_tracked(
        {
            "AAPL": {
                "qty": 5,
                "entry_price": 100.0,
                "adds_et_date": "2026-06-01",
                "adds_et_date_count": 1,
            }
        },
        "u1",
        data_dir=tmp_path,
    )
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "AAPL", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    recorder = _TerminalRecorder()
    broker = MagicMock()
    broker._sqlite_event_store = recorder
    broker.get_open_orders.return_value = []
    broker.get_latest_quote.return_value = _Quote(mid=100.0, spread_pct=0.1)
    broker.submit_order.return_value = MagicMock(id="order-core")
    broker.get_positions.return_value = [
        {"symbol": "AAPL", "market_value": 600.0, "qty": 6, "avg_price": 100.0}
    ]
    engine = MagicMock()
    engine.execution.build_order_from_dict.return_value = MagicMock()
    engine.strategy.stop_loss_pct = 1.5

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "AAPL",
                    "score": 0.25,
                    "route": "core_rebuild",
                    "source": "core_rebuild",
                    "core_rebuild": True,
                    "signal_score": 1.3,
                }
            ],
            broker=broker,
            engine=engine,
            config={"options": {"enabled": False}},
            dt=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            positions=[{"symbol": "AAPL", "market_value": 500.0, "qty": 5}],
            tracked={},
            current_positions={},
            eligible_active=["AAPL"],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": False,
                "fallback_on_empty_alloc": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2026-06-01",
            cycle_risk_state=None,
            verbose=False,
        )

    broker.submit_order.assert_called_once()
    assert any(
        row["symbol"] == "AAPL" and row["stage"] == "submitted"
        for row in recorder.rows
    )
    assert "allocator add-on already used today" not in caplog.text
    assert "ALLOCATOR_ACTION_CREATED symbol=AAPL action=buy notional=1200.00 route=core_rebuild" in caplog.text
    assert "EXECUTION_COOLDOWN_STATE symbol=AAPL" in caplog.text
    assert "add_on_used_today=True" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMIT_ATTEMPT symbol=AAPL action=buy notional=1200.00 route=core_rebuild" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=AAPL action=buy notional=1200.00 order_id=order-core route=core_rebuild" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_allocator_action_logs_post_check_exit_when_order_build_returns_none(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "SMH", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    broker.get_open_orders.return_value = []
    broker.get_latest_quote.return_value = _Quote(mid=100.0, spread_pct=0.1)
    broker.get_positions.return_value = []
    engine = MagicMock()
    engine.execution.build_order_from_dict.return_value = None

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "SMH",
                    "score": 0.25,
                    "route": "core_rebuild",
                    "source": "core_rebuild",
                    "core_rebuild": True,
                    "signal_score": 90.0,
                }
            ],
            broker=broker,
            engine=engine,
            config={"options": {"enabled": False}},
            dt=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=["SMH"],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": False,
                "fallback_on_empty_alloc": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2026-06-01",
            cycle_risk_state=None,
            verbose=False,
        )

    out = capsys.readouterr().out
    assert "ALLOCATOR ORDER CHECK SMH:" in out
    assert "passes? True" in out
    broker.submit_order.assert_not_called()
    assert "ALLOCATOR_ACTION_SUBMIT_ATTEMPT symbol=SMH" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=SMH reason=execution_blocked" in caplog.text
    assert (
        "ALLOCATOR_ACTION_POST_CHECK_EXIT symbol=SMH "
        "reason=order_build_or_execution_blocked"
    ) in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_allocator_action_still_uses_dynamic_guards(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "BBCP", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    recorder = _TerminalRecorder()
    broker = MagicMock()
    broker._sqlite_event_store = recorder
    broker.get_open_orders.return_value = []
    broker.get_latest_quote.return_value = _Quote(mid=10.0, spread_pct=0.1)
    engine = MagicMock()
    engine.execution.build_order_from_dict.return_value = MagicMock()

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "BBCP",
                    "score": 9.0,
                    "source": "dynamic_universe",
                    "dynamic_candidate": True,
                    "news_score": 7.0,
                    "event_score": 7.0,
                    "catalyst_score": 0.7,
                    "relative_volume": 0.1,
                }
            ],
            broker=broker,
            engine=engine,
            config={
                "options": {"enabled": False},
                "dynamic_universe": {"min_relative_volume": 1.0},
            },
            dt=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": False,
                "fallback_on_empty_alloc": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2026-06-01",
            cycle_risk_state=None,
            verbose=False,
        )

    broker.submit_order.assert_not_called()
    assert any(
        row["symbol"] == "BBCP"
        and row["stage"] == "order_builder_rejected"
        and row["reason"] == "dynamic_relative_volume"
        for row in recorder.rows
    )
    assert "ALLOCATOR_ACTION_CREATED symbol=BBCP action=buy notional=1200.00 route=dynamic_universe" in caplog.text
    assert (
        "ALLOCATOR_DYNAMIC_RVOL_CHECK symbol=BBCP route=dynamic_universe "
        "relative_volume=0.100 required_relative_volume=1.000"
    ) in caplog.text
    assert "catalyst_score=0.70 event_score=7.00 news_score=7.00 bypass=false" in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=BBCP reason=dynamic_relative_volume" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMIT_ATTEMPT symbol=BBCP" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_premarket_catalyst_replay_bypasses_zero_rvol_order_builder_guard(
    mock_allocator_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[{"action": "buy", "symbol": "QQQ", "notional": 1200.0}])
    mock_allocator_cls.return_value = inst
    recorder = _TerminalRecorder()
    broker = MagicMock()
    broker._sqlite_event_store = recorder
    broker.get_open_orders.return_value = []
    broker.get_latest_quote.return_value = _Quote(mid=400.0, spread_pct=0.1)
    broker.submit_order.return_value = MagicMock(id="order-qqq")
    engine = MagicMock()
    engine.execution.build_order_from_dict.return_value = MagicMock()

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "QQQ",
                    "score": 9.0,
                    "source": "premarket_catalyst_replay",
                    "route": "premarket_catalyst_replay",
                    "dynamic_candidate": True,
                    "news_score": 3.0,
                    "event_score": 3.0,
                    "catalyst_score": 0.3,
                    "relative_volume": 0.0,
                }
            ],
            broker=broker,
            engine=engine,
            config={
                "options": {"enabled": False},
                "dynamic_universe": {"min_relative_volume": 1.0},
            },
            dt=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": False,
                "fallback_on_empty_alloc": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2026-06-01",
            cycle_risk_state=None,
            verbose=False,
        )

    broker.submit_order.assert_called_once()
    assert any(
        row["symbol"] == "QQQ"
        and row["stage"] == "submitted"
        and row["reason"] == "submitted"
        for row in recorder.rows
    )
    assert "ALLOCATOR_ACTION_CREATED symbol=QQQ action=buy notional=1200.00 route=premarket_catalyst_replay" in caplog.text
    assert (
        "ALLOCATOR_DYNAMIC_RVOL_CHECK symbol=QQQ route=premarket_catalyst_replay "
        "relative_volume=0.000 required_relative_volume=1.000"
    ) in caplog.text
    assert "catalyst_score=0.30 event_score=3.00 news_score=3.00 bypass=true" in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=QQQ reason=dynamic_relative_volume" not in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=QQQ action=buy notional=1200.00" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_momentum_override_entry_approval_bypasses_dispatch_rvol_mismatch(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "AMD",
                "notional": 4636.5604,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="paper-amd")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "AMD",
            "symbol": "AMD",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "relative_volume_confirmed": False,
            "volume_confirmed": False,
            "relative_volume": 0.62,
            "news_score": 8.0,
            "event_score": 7.0,
            "catalyst_score": 0.82,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=120.0, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 1.0},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert (
        "DISPATCH_DYNAMIC_RVOL_DECISION symbol=AMD route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true observed_rvol=0.620 "
        "required_rvol=1.000 scanner_relative_volume=0.620 entry_relative_volume=0.620 "
        "allocator_relative_volume=0.620 execution_relative_volume=0.620 "
        "dispatch_relative_volume=0.000 scanner_threshold=1.000 entry_threshold=1.000 "
        "allocator_threshold=1.000 dispatch_threshold=1.000 threshold_used=1.000 "
        "rejected_component=none upstream_approved=true upstream_reason=dynamic_momentum_override_entry_approved "
        "catalyst_override_active=false "
        "pure_momentum_override_active=false override_active=true "
        "skip_or_allow_reason=paper_dynamic_momentum_override_entry_approved"
    ) in caplog.text
    assert (
        "ALLOCATOR_DYNAMIC_RVOL_CHECK symbol=AMD route=dynamic_momentum_override "
        "relative_volume=0.620 required_relative_volume=1.000"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=AMD reason=dynamic_relative_volume" not in caplog.text
    assert "ORDER_SKIP symbol=AMD reason=dynamic_relative_volume source=capital_allocator" not in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=AMD action=buy notional=4000.00" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_dispatch_uses_preserved_scanner_rvol_when_execution_field_is_stale(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "CSCO",
                "notional": 4634.97,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="paper-csco")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "CSCO",
            "symbol": "CSCO",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "relative_volume": 0.0,
            "scanner_relative_volume": 1.24,
            "allocator_relative_volume": 1.24,
            "gain_pct": 4.5,
            "day_gain_pct": 4.5,
            "dynamic_score": 87.0,
            "scanner_score": 87.0,
            "signal_score": 87.0,
            "news_score": 4.0,
            "event_score": 4.0,
            "catalyst_score": 0.4,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=58.0, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 1.0},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ORDER_SKIP symbol=CSCO reason=dynamic_relative_volume" not in caplog.text
    assert (
        "DISPATCH_DYNAMIC_RVOL_DECISION symbol=CSCO route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true observed_rvol=1.240 "
        "required_rvol=1.000 scanner_relative_volume=1.240 entry_relative_volume=1.240 "
        "allocator_relative_volume=1.240 execution_relative_volume=0.000 "
        "dispatch_relative_volume=0.000 scanner_threshold=1.000 entry_threshold=1.000 "
        "allocator_threshold=1.000 dispatch_threshold=1.000 threshold_used=1.000"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=CSCO action=buy" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_news_candidate_entry_pass_not_rejected_by_dispatch_base_rvol(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "INTC",
                "notional": 4635.0,
                "source": "dynamic_universe",
                "route": "dynamic_universe",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="paper-intc")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "INTC",
            "symbol": "INTC",
            "score": 62.0,
            "strength_eff": 62.0,
            "source": "dynamic_universe",
            "route": "dynamic_universe",
            "entry_eval_route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "relative_volume": 0.882,
            "allocator_relative_volume": 0.882,
            "gain_pct": 3.8,
            "day_gain_pct": 3.8,
            "dynamic_score": 62.0,
            "scanner_score": 62.0,
            "signal_score": 62.0,
            "news_score": 8.0,
            "event_score": 7.5,
            "catalyst_score": 0.8,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=33.0, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 1.0},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ORDER_SKIP symbol=INTC reason=dynamic_relative_volume source=capital_allocator" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=INTC reason=dynamic_relative_volume" not in caplog.text
    assert (
        "DISPATCH_DYNAMIC_RVOL_DECISION symbol=INTC route=dynamic_universe "
        "source=dynamic_universe dynamic_candidate=true observed_rvol=0.882 "
        "required_rvol=1.000 scanner_relative_volume=0.882 entry_relative_volume=0.882 "
        "allocator_relative_volume=0.882 execution_relative_volume=0.882 "
        "dispatch_relative_volume=0.000 scanner_threshold=1.000 entry_threshold=1.000 "
        "allocator_threshold=1.000 dispatch_threshold=1.000 threshold_used=1.000 "
        "rejected_component=none upstream_approved=true upstream_reason=dynamic_momentum_override_entry_approved "
        "catalyst_override_active=false pure_momentum_override_active=false "
        "override_active=true skip_or_allow_reason=paper_dynamic_momentum_override_entry_approved"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=INTC action=buy" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_dispatch_honors_upstream_vwap_approval_when_dispatch_vwap_is_stale(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "AMKR",
                "notional": 4636.94,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="paper-amkr")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "AMKR",
            "symbol": "AMKR",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "relative_volume": 1.695,
            "allocator_relative_volume": 1.695,
            "gain_pct": 6.95,
            "news_score": 9.91,
            "event_score": 9.0,
            "catalyst_score": 0.991,
            "price_above_vwap": True,
            "vwap_above": True,
            "paper_current_price": 26.40,
            "paper_session_vwap": 26.55,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=26.40, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 1.0},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ORDER_SKIP symbol=AMKR reason=dynamic_vwap" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=AMKR reason=dynamic_vwap" not in caplog.text
    assert (
        "DISPATCH_DYNAMIC_VWAP_CHECK symbol=AMKR route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true price=26.4000 vwap=26.5500 "
        "distance_from_vwap_pct=-0.565 threshold_pct=0.000 scanner_vwap_above=n/a "
        "entry_vwap_above=true allocator_vwap_above=n/a upstream_vwap_approved=true "
        "entry_approved=true catalyst_override_active=false pure_momentum_override_active=false "
        "override_active=true dispatch_result=allowed "
        "skip_or_allow_reason=paper_dynamic_entry_vwap_approved"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=AMKR action=buy" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_dispatch_honors_entry_approval_when_vwap_flag_is_missing(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "AAL",
                "notional": 4635.202,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="paper-aal")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "AAL",
            "symbol": "AAL",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "relative_volume": 0.7548560501565609,
            "allocator_relative_volume": 0.7548560501565609,
            "gain_pct": 2.845134173941144,
            "news_score": 9.0466,
            "event_score": 9.0,
            "catalyst_score": 0.90466,
            "paper_current_price": 15.885000000000002,
            "paper_session_vwap": 15.976816087047807,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=15.885, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 0.7},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ORDER_SKIP symbol=AAL reason=dynamic_vwap" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=AAL reason=dynamic_vwap" not in caplog.text
    assert (
        "DISPATCH_DYNAMIC_VWAP_CHECK symbol=AAL route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true price=15.8850 vwap=15.9768 "
        "distance_from_vwap_pct=-0.575 threshold_pct=0.000 scanner_vwap_above=n/a "
        "entry_vwap_above=n/a allocator_vwap_above=n/a upstream_vwap_approved=false "
        "entry_approved=true catalyst_override_active=false pure_momentum_override_active=false "
        "override_active=true dispatch_result=allowed "
        "skip_or_allow_reason=paper_dynamic_entry_vwap_approved"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=AAL action=buy" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_paper_dynamic_bold_above_scanner_min_price_not_dispatch_price_skipped(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "BOLD",
                "notional": 1312.50,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="paper-bold")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["signals"] = [
        {
            "sym_u": "BOLD",
            "symbol": "BOLD",
            "score": 55.0,
            "scanner_score": 55.0,
            "dynamic_score": 55.0,
            "strength_eff": 55.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "relative_volume": 1.12,
            "allocator_relative_volume": 1.12,
            "gain_pct": 8.2,
            "news_score": 9.1,
            "event_score": 8.8,
            "catalyst_score": 0.91,
            "paper_current_price": 2.55,
            "price_above_vwap": True,
            "vwap_above": True,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=2.55, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": True},
        "options": {"enabled": False},
        "dynamic_universe": {"min_price": 2.0, "min_relative_volume": 0.7},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ORDER_SKIP symbol=BOLD reason=dynamic_price_below_minimum" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=BOLD reason=dynamic_price_below_minimum" not in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=BOLD action=buy" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_dynamic_price_floor_remains_conservative_with_diagnostics(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "BOLD",
                "notional": 1312.50,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [
        {
            "sym_u": "BOLD",
            "symbol": "BOLD",
            "score": 55.0,
            "scanner_score": 55.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "relative_volume": 1.12,
            "allocator_relative_volume": 1.12,
            "gain_pct": 8.2,
            "news_score": 9.1,
            "catalyst_score": 0.91,
            "paper_current_price": 2.55,
            "price_above_vwap": True,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=2.55, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {"min_price": 2.0, "min_relative_volume": 0.7},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "ORDER_SKIP symbol=BOLD reason=dynamic_price_below_minimum source=capital_allocator" in caplog.text
    assert "observed_price=2.5500" in caplog.text
    assert "min_price=5.0000" in caplog.text
    assert "price_source=candidate" in caplog.text
    assert "route=dynamic_momentum_override" in caplog.text
    assert "is_dynamic=true" in caplog.text
    assert "scanner_score=55.0" in caplog.text
    assert "news_score=9.1" in caplog.text
    assert "catalyst_score=0.91" in caplog.text


def test_dynamic_scanner_and_execution_min_price_do_not_drift() -> None:
    cfg = {"dynamic_universe": {"min_price": 2.0, "min_relative_volume": 0.7}}

    assert _dynamic_scan_settings(
        dict(cfg["dynamic_universe"], broker_is_paper=False)
    )["min_price"] == pytest.approx(
        _dynamic_min_price_from_config(cfg, broker_is_paper=False)
    )
    assert _dynamic_scan_settings(
        dict(cfg["dynamic_universe"], broker_is_paper=True)
    )["min_price"] == pytest.approx(
        _dynamic_min_price_from_config(cfg, broker_is_paper=True)
    )


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_dynamic_momentum_override_uses_entry_rvol_when_dispatch_rvol_is_stale(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "AAPL",
                "notional": 4100.0,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="live-aapl")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [
        {
            "sym_u": "AAPL",
            "symbol": "AAPL",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "final": True,
            "relative_volume": 0.80,
            "scanner_relative_volume": 0.80,
            "entry_relative_volume": 0.80,
            "allocator_relative_volume": 0.80,
            "execution_relative_volume": 0.20,
            "gain_pct": 4.2,
            "day_gain_pct": 4.2,
            "dynamic_score": 41.0,
            "scanner_score": 41.0,
            "signal_score": 41.0,
            "news_score": 4.0,
            "event_score": 4.0,
            "catalyst_score": 0.4,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=200.0, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 0.60},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ALLOCATOR_ACTION_CREATED symbol=AAPL action=buy notional=4100.00 route=dynamic_momentum_override" in caplog.text
    assert "ALLOCATOR_DISPATCH_START symbol=AAPL action=buy notional=4000.00" in caplog.text
    assert "ORDER_SKIP symbol=AAPL reason=dynamic_relative_volume source=capital_allocator" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=AAPL reason=dynamic_relative_volume" not in caplog.text
    assert (
        "DISPATCH_DYNAMIC_RVOL_DECISION symbol=AAPL route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true observed_rvol=0.800 "
        "required_rvol=0.600 scanner_relative_volume=0.800 entry_relative_volume=0.800 "
        "allocator_relative_volume=0.800 execution_relative_volume=0.200 "
        "dispatch_relative_volume=0.200 scanner_threshold=0.600 entry_threshold=0.600 "
        "allocator_threshold=0.600 dispatch_threshold=0.600 threshold_used=0.600 "
        "rejected_component=none upstream_approved=true upstream_reason=dynamic_momentum_override_entry_approved "
        "catalyst_override_active=false pure_momentum_override_active=false "
        "override_active=true skip_or_allow_reason=live_dynamic_momentum_override_entry_approved"
    ) in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=AAPL action=buy notional=4000.00" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_momentum_expectancy_gate_caps_negative_expectancy_notional(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _write_signal_expectancy_report(tmp_path, day="2026-06-11", symbol="INTC", count=6, score=-0.75)
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_dynamic_allocator_action("INTC", 1200.0)])
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="live-intc")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {
            "min_relative_volume": 1.0,
            "dynamic_momentum_expectancy_gate": {
                "enabled": True,
                "live_enabled": True,
                "min_expectancy_score": 0.0,
                "lookback_days": 5,
                "min_samples": 5,
                "reduce_only_when_negative": True,
                "fallback_allow_if_no_data": True,
                "max_notional_when_negative": 300,
            },
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    action = mock_place_order.call_args.args[2]
    assert action["notional"] == pytest.approx(300.0)
    assert "DYNAMIC_EXPECTANCY_GATE_REDUCE symbol=INTC route=dynamic_momentum_override" in caplog.text
    assert "expectancy_score=-0.7500" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=INTC action=buy notional=300.00" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_momentum_expectancy_gate_no_data_allows_unchanged(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_dynamic_allocator_action("INTC", 1200.0)])
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="live-intc")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {
            "min_relative_volume": 1.0,
            "dynamic_momentum_expectancy_gate": {
                "enabled": True,
                "live_enabled": True,
                "fallback_allow_if_no_data": True,
                "max_notional_when_negative": 300,
            },
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert mock_place_order.call_args.args[2]["notional"] == pytest.approx(1200.0)
    assert "DYNAMIC_EXPECTANCY_GATE_REDUCE" not in caplog.text
    assert "DYNAMIC_EXPECTANCY_GATE_BLOCK" not in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_momentum_expectancy_gate_blocked_symbol_skips_buy(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_dynamic_allocator_action("ARCT", 1200.0)])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [_paper_dynamic_signal("ARCT")]
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {
            "min_relative_volume": 1.0,
            "dynamic_momentum_expectancy_gate": {
                "enabled": True,
                "live_enabled": True,
                "blocked_symbols": ["ARCT"],
            },
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "DYNAMIC_EXPECTANCY_GATE_BLOCK symbol=ARCT route=dynamic_momentum_override" in caplog.text
    assert "ORDER_SKIP symbol=ARCT reason=dynamic_expectancy_gate_block source=capital_allocator" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_dynamic_momentum_expectancy_gate_does_not_affect_trend_options_or_sells(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _write_signal_expectancy_report(tmp_path, day="2026-06-11", symbol="SPY", route="dynamic_momentum_override", count=10, score=-2.0)
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {"action": "buy", "symbol": "SPY", "notional": 1200.0, "route": "trend_long", "source": "scoring"},
            {"action": "buy", "symbol": "QQQ260417P00567000", "notional": 1200.0, "route": "dynamic_momentum_override", "source": "options", "dynamic_candidate": True},
            {"action": "sell", "symbol": "RIVN", "notional": 1200.0, "route": "dynamic_momentum_override", "source": "dynamic_universe", "dynamic_candidate": True},
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="order-ok")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [
        {"sym_u": "SPY", "symbol": "SPY", "route": "trend_long", "source": "scoring", "score": 10.0},
        _paper_dynamic_signal("QQQ260417P00567000"),
        _paper_dynamic_signal("RIVN"),
    ]
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": True},
        "dynamic_universe": {
            "min_relative_volume": 1.0,
            "dynamic_momentum_expectancy_gate": {
                "enabled": True,
                "live_enabled": True,
                "blocked_symbols": ["RIVN"],
                "max_notional_when_negative": 300,
            },
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    notionals = [call.args[2]["notional"] for call in mock_place_order.call_args_list]
    assert notionals == [pytest.approx(1200.0), pytest.approx(1200.0), pytest.approx(1200.0)]
    assert "DYNAMIC_EXPECTANCY_GATE_REDUCE" not in caplog.text
    assert "DYNAMIC_EXPECTANCY_GATE_BLOCK" not in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_trend_reentry_protection_live_disabled_unchanged(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _record_trend_stop(tmp_path, timestamp=datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc))
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_trend_action("JPM", notional=1200.0)])
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="trend-jpm")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["dt"] = datetime(2026, 6, 11, 10, 30, tzinfo=timezone.utc)
    kwargs["signals"] = [_trend_signal("JPM")]
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "trend_long": {
            "reentry_protection": {
                "enabled": True,
                "live_enabled": False,
                "cooldown_minutes_after_stop": 90,
            }
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "TREND_REENTRY_PROTECTION_BLOCK" not in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_trend_reentry_protection_live_enabled_blocks_after_stop(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _record_trend_stop(tmp_path, timestamp=datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc))
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_trend_action("JPM", notional=1200.0)])
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["dt"] = datetime(2026, 6, 11, 10, 30, tzinfo=timezone.utc)
    kwargs["signals"] = [_trend_signal("JPM", signal_timestamp="2026-06-11T10:20:00+00:00")]
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "trend_long": {
            "reentry_protection": {
                "enabled": True,
                "live_enabled": True,
                "cooldown_minutes_after_stop": 90,
                "require_new_breakout": True,
                "require_new_signal_timestamp": True,
            }
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "TREND_REENTRY_PROTECTION_CHECK symbol=JPM action=buy route=trend_long decision=block reason=cooldown_active" in caplog.text
    assert "TREND_REENTRY_PROTECTION_BLOCK symbol=JPM reason=cooldown_active" in caplog.text
    assert "ORDER_SKIP symbol=JPM reason=trend_reentry_protection source=capital_allocator" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_trend_reentry_protection_cooldown_expiry_allows_reentry(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _record_trend_stop(tmp_path, timestamp=datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc))
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_trend_action("JPM", notional=1200.0)])
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="trend-jpm")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["dt"] = datetime(2026, 6, 11, 11, 40, tzinfo=timezone.utc)
    kwargs["signals"] = [_trend_signal("JPM")]
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "trend_long": {
            "reentry_protection": {
                "enabled": True,
                "live_enabled": True,
                "cooldown_minutes_after_stop": 90,
                "require_new_breakout": False,
                "require_new_intraday_high": False,
                "require_new_signal_timestamp": False,
            }
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "TREND_REENTRY_PROTECTION_EXPIRED symbol=JPM" in caplog.text
    assert "TREND_REENTRY_PROTECTION_ALLOW symbol=JPM reason=expired_with_fresh_signal" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_trend_reentry_protection_fresh_breakout_allows_after_cooldown(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _record_trend_stop(tmp_path, timestamp=datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc))
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[_trend_action("JPM", notional=1200.0)])
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="trend-jpm")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["dt"] = datetime(2026, 6, 11, 11, 40, tzinfo=timezone.utc)
    kwargs["signals"] = [_trend_signal("JPM", signal_timestamp=None, five_min_breakout=True)]
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "trend_long": {
            "reentry_protection": {
                "enabled": True,
                "live_enabled": True,
                "cooldown_minutes_after_stop": 90,
                "require_new_breakout": True,
                "require_new_intraday_high": False,
                "require_new_signal_timestamp": True,
            }
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "TREND_REENTRY_PROTECTION_ALLOW symbol=JPM reason=expired_with_fresh_signal" in caplog.text
    assert "breakout=true" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_trend_reentry_protection_does_not_affect_dynamic_options_exits(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _record_trend_stop(tmp_path, timestamp=datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc))
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            _dynamic_allocator_action("JPM", 1200.0),
            {"action": "buy", "symbol": "QQQ260417P00567000", "notional": 1200.0, "route": "trend_long", "source": "options"},
            _trend_action("JPM", action="sell", notional=500.0),
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="ok")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["dt"] = datetime(2026, 6, 11, 10, 30, tzinfo=timezone.utc)
    kwargs["ca_cfg"]["consolidate_net_before_submit"] = False
    kwargs["signals"] = [
        _paper_dynamic_signal("JPM"),
        _trend_signal("QQQ260417P00567000"),
        _trend_signal("JPM"),
    ]
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": True},
        "dynamic_universe": {"min_relative_volume": 1.0},
        "trend_long": {
            "reentry_protection": {
                "enabled": True,
                "live_enabled": True,
                "cooldown_minutes_after_stop": 90,
            }
        },
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    assert mock_place_order.call_count == 3
    assert "TREND_REENTRY_PROTECTION_BLOCK" not in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_dynamic_momentum_override_non_fastlane_below_effective_rvol_skips_dispatch(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "AVGO",
                "notional": 4100.0,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [
        {
            "sym_u": "AVGO",
            "symbol": "AVGO",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "final": True,
            "relative_volume": 0.35,
            "scanner_relative_volume": 0.35,
            "entry_relative_volume": 0.35,
            "allocator_relative_volume": 0.35,
            "execution_relative_volume": 0.35,
            "effective_min_rel_volume": 0.60,
            "scanner_effective_min_rel_volume": 0.60,
            "entry_effective_min_rel_volume": 0.60,
            "catalyst_fastlane_active": False,
            "catalyst_min_relative_volume": 0.35,
            "gain_pct": 4.2,
            "day_gain_pct": 4.2,
            "dynamic_score": 41.0,
            "scanner_score": 41.0,
            "signal_score": 41.0,
            "news_score": 7.0,
            "event_score": 7.0,
            "catalyst_score": 0.70,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=900.0, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 0.60},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "ORDER_SKIP symbol=AVGO reason=dynamic_relative_volume source=capital_allocator" in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=AVGO reason=dynamic_relative_volume" in caplog.text
    assert (
        "DISPATCH_DYNAMIC_RVOL_DECISION symbol=AVGO route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true observed_rvol=0.350 "
        "required_rvol=0.600"
    ) in caplog.text
    assert "catalyst_override_active=false pure_momentum_override_active=false override_active=false" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_dynamic_fastlane_uses_effective_rvol_threshold_at_dispatch(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "ORCL",
                "notional": 4100.0,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    mock_place_order.return_value = MagicMock(id="live-orcl")
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [
        {
            "sym_u": "ORCL",
            "symbol": "ORCL",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "final": True,
            "relative_volume": 0.36,
            "scanner_relative_volume": 0.36,
            "entry_relative_volume": 0.36,
            "allocator_relative_volume": 0.36,
            "execution_relative_volume": 0.20,
            "effective_min_rel_volume": 0.35,
            "scanner_effective_min_rel_volume": 0.35,
            "entry_effective_min_rel_volume": 0.35,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
            "gain_pct": 4.2,
            "day_gain_pct": 4.2,
            "dynamic_score": 41.0,
            "scanner_score": 41.0,
            "signal_score": 41.0,
            "news_score": 8.0,
            "event_score": 7.0,
            "catalyst_score": 0.82,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=200.0, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 0.60},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_called_once()
    assert "ORDER_SKIP symbol=ORCL reason=dynamic_relative_volume source=capital_allocator" not in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=ORCL reason=dynamic_relative_volume" not in caplog.text
    assert (
        "DISPATCH_DYNAMIC_RVOL_DECISION symbol=ORCL route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true observed_rvol=0.360 "
        "required_rvol=0.350"
    ) in caplog.text
    assert "threshold_used=0.350" in caplog.text
    assert "ALLOCATOR_ACTION_SUBMITTED symbol=ORCL action=buy notional=4000.00" in caplog.text


@patch("src.capital_allocator_loop.place_order")
@patch("src.capital_allocator_loop.CapitalAllocator")
def test_live_dynamic_fastlane_still_rejects_below_effective_rvol_threshold(
    mock_allocator_cls: MagicMock,
    mock_place_order: MagicMock,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(
        return_value=[
            {
                "action": "buy",
                "symbol": "FAST",
                "notional": 4100.0,
                "source": "dynamic_universe",
                "route": "dynamic_momentum_override",
                "dynamic_candidate": True,
            }
        ]
    )
    mock_allocator_cls.return_value = inst
    kwargs = _allocator_dispatch_common_kwargs(tmp_path)
    kwargs["user_id"] = "live_bot"
    kwargs["signals"] = [
        {
            "sym_u": "FAST",
            "symbol": "FAST",
            "score": 2.0,
            "strength_eff": 2.0,
            "source": "dynamic_universe",
            "route": "dynamic_momentum_override",
            "dynamic_candidate": True,
            "dynamic_symbol": True,
            "entry_eval_final": True,
            "decision_allowed": True,
            "final": True,
            "relative_volume": 0.20,
            "scanner_relative_volume": 0.20,
            "entry_relative_volume": 0.20,
            "allocator_relative_volume": 0.20,
            "execution_relative_volume": 0.20,
            "effective_min_rel_volume": 0.35,
            "scanner_effective_min_rel_volume": 0.35,
            "entry_effective_min_rel_volume": 0.35,
            "catalyst_fastlane_active": True,
            "catalyst_min_relative_volume": 0.35,
            "gain_pct": 4.2,
            "day_gain_pct": 4.2,
            "dynamic_score": 41.0,
            "scanner_score": 41.0,
            "signal_score": 41.0,
            "news_score": 8.0,
            "event_score": 7.0,
            "catalyst_score": 0.82,
        }
    ]
    kwargs["broker"].get_latest_quote.return_value = _Quote(mid=20.0, spread_pct=0.1)
    kwargs["config"] = {
        "broker": {"paper": False},
        "options": {"enabled": False},
        "dynamic_universe": {"min_relative_volume": 0.60},
    }

    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(**kwargs)

    mock_place_order.assert_not_called()
    assert "ORDER_SKIP symbol=FAST reason=dynamic_relative_volume source=capital_allocator" in caplog.text
    assert "ALLOCATOR_ACTION_BLOCKED symbol=FAST reason=dynamic_relative_volume" in caplog.text
    assert (
        "DISPATCH_DYNAMIC_RVOL_DECISION symbol=FAST route=dynamic_momentum_override "
        "source=dynamic_universe dynamic_candidate=true observed_rvol=0.200 "
        "required_rvol=0.350"
    ) in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_no_fallback_when_allow_buys_false(
    mock_allocator_cls: MagicMock, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    """Gross / gate blocks: ``allow_effective`` false → no fallback BUY even if ``allocate`` is []."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    with caplog.at_level("WARNING", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[{"sym_u": "A", "composite_score": 5.0, "notional": 2000.0}],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=False,
        )
    out = capsys.readouterr().out
    assert "ALLOCATOR ACTIONS: []" in out
    assert "allocator plan empty" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_no_fallback_when_disabled(
    mock_allocator_cls: MagicMock, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    with caplog.at_level("WARNING", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "A", "composite_score": 5.0, "notional": 2000.0},
            ],
            broker=MagicMock(),
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "fallback_on_empty_alloc": False,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
        )
    out = capsys.readouterr().out
    assert "ALLOCATOR ACTIONS: []" in out
    assert "allocator plan empty" not in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_single_candidate_forces_minimum_trade_when_fallback_disabled(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """When equal-split fallback is off and allocate() returns [], one candidate still gets a BUY."""
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    quote = MagicMock()
    quote.reference_mid = MagicMock(return_value=55.0)
    quote.mid = 55.0
    broker = MagicMock()
    broker.get_latest_quote = MagicMock(return_value=quote)
    with caplog.at_level("WARNING", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[{"sym_u": "KO", "composite_score": 0.6}],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 500.0,
                "rotate_trim_fraction": 0.3,
                "fallback_on_empty_alloc": False,
                "prioritize_diversification": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )
    out = capsys.readouterr().out
    assert "ALLOCATOR ACTIONS: []" in out
    #assert "FORCING MINIMUM TRADE (single candidate fix)" in out
    assert "single candidate, allocator empty" in caplog.text.lower()
    assert "KO" in out
    assert "single_candidate_minimum_trade" in out
    assert "ALLOCATOR ACTIONS:" in out


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_selected_candidates_force_top_ranked_when_plan_empty(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    quote = MagicMock()
    quote.reference_mid = MagicMock(return_value=80.0)
    quote.mid = 80.0
    broker = MagicMock()
    broker.get_latest_quote = MagicMock(return_value=quote)
    with caplog.at_level("WARNING", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {"sym_u": "CAT", "composite_score": 0.9},
                {"sym_u": "SMH", "composite_score": 0.8},
                {"sym_u": "KO", "composite_score": 0.7},
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 500.0,
                "rotate_trim_fraction": 0.3,
                "fallback_on_empty_alloc": False,
                "prioritize_diversification": False,
                "selected_must_execute": True,
            },
            user_id="u-selected",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state=None,
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )
    out = capsys.readouterr().out
    assert "selected: ['CAT', 'SMH', 'KO']" in out
    assert "FORCING MINIMUM TRADE (selected must execute)" in out
    assert "selected_must_execute_force_buy" in out
    assert "'symbol': 'CAT'" in out
    assert "ALLOCATOR ACTIONS: []" in out
    #assert "selected-must-execute fallback" in caplog.text


@patch("src.capital_allocator_loop.CapitalAllocator")
def test_execute_allocator_allows_no_trade_cycle_when_selected_not_must_execute(
    mock_allocator_cls: MagicMock,
    capsys: pytest.CaptureFixture,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _ALLOCATOR_EMPTY_ACTION_CYCLES.clear()
    inst = MagicMock()
    inst.allocate = MagicMock(return_value=[])
    mock_allocator_cls.return_value = inst
    broker = MagicMock()
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "BBCP",
                    "composite_score": 0.9,
                    "source": "dynamic_universe",
                    "news_score": 7.0,
                    "event_score": 7.0,
                    "catalyst_score": 0.7,
                },
                {"sym_u": "SMH", "composite_score": 0.8},
                {"sym_u": "KO", "composite_score": 0.7},
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[],
            tracked={},
            current_positions={},
            eligible_active=[],
            account_equity=100_000.0,
            cash=25_000.0,
            ca_cfg={
                "max_positions": 5,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 500.0,
                "rotate_trim_fraction": 0.3,
                "fallback_on_empty_alloc": False,
                "if_no_actions_cycles": 1,
                "fallback_pick_top_n": 2,
                "fallback_size_pct": 0.1,
                "prioritize_diversification": False,
                "allow_no_trade_cycles": True,
                "selected_must_execute": False,
            },
            user_id="u-no-trade",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state={"daily_loss_lockout": False},
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )
    out = capsys.readouterr().out
    assert "selected: ['BBCP', 'SMH', 'KO']" in out
    assert "no trade cycle allowed" in out
    assert "FORCING MINIMUM TRADE" not in out
    assert "idle fallback" not in caplog.text
    assert "ALLOCATOR_SKIP_REASON symbol=BBCP reason=no_trade_cycle_allowed" in caplog.text
    assert "trade_cycle_allowed=True" in caplog.text
    assert "cooldown_active=False" in caplog.text
    assert "next_eligible_entry_time=n/a" in caplog.text
    assert "allocator_lockout_allow_effective=True" in caplog.text
    assert "daily_loss_lockout_active=False" in caplog.text
    assert "dynamic_position_slots_remaining=6" in caplog.text
    broker.get_latest_quote.assert_not_called()


def test_execute_allocator_logs_no_action_detail_for_selected_dynamic_candidate(
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    _ALLOCATOR_EMPTY_ACTION_CYCLES.clear()
    broker = MagicMock()
    with caplog.at_level("INFO"):
        execute_capital_allocator_pass(
            signals=[
                {
                    "sym_u": "BBCP",
                    "composite_score": 9.0,
                    "strength_eff": 0.20,
                    "source": "dynamic_universe",
                    "news_score": 7.0,
                    "event_score": 7.0,
                    "catalyst_score": 0.7,
                },
            ],
            broker=broker,
            engine=MagicMock(),
            config={"options": {"enabled": False}, "portfolio": {"target_cash_pct": 0}},
            dt=MagicMock(strftime=MagicMock(return_value="10:00")),
            positions=[{"symbol": "OLD", "market_value": 1_000.0}],
            tracked={"OLD": {"signal_strength": 0.80}},
            current_positions={},
            eligible_active=["OLD"],
            account_equity=100_000.0,
            cash=0.0,
            ca_cfg={
                "max_positions": 1,
                "symbol_cap": 0.25,
                "min_trade_size": 500.0,
                "min_realloc_leg": 300.0,
                "rotate_trim_fraction": 0.3,
                "require_net_sell_gte_buy": True,
                "fallback_on_empty_alloc": False,
                "allow_no_trade_cycles": True,
                "force_minimum_trade_single_candidate": False,
            },
            user_id="u1",
            data_dir=tmp_path,
            stale_quote_max_age=60.0,
            strength_jitter_max=0.0,
            et_date_iso="2024-01-02",
            cycle_risk_state={"daily_loss_lockout": False},
            verbose=False,
            allow_allocator_buys=True,
            gross_exposure_pct=50.0,
        )

    out = capsys.readouterr().out
    assert "selected: ['BBCP']" in out
    assert "no trade cycle allowed" in out
    assert "ALLOCATOR_NO_ACTION_DETAIL symbol=BBCP reason=rotation_not_stronger" in caplog.text
    assert "final=True" in caplog.text
    assert "target_allocation=25000.00" in caplog.text
    assert "available_cash=0.00" in caplog.text
    assert "cash_reserve=0.00" in caplog.text
    assert "candidate_notional_requested=500.00" in caplog.text
    assert "candidate_notional=500.00" in caplog.text
    assert "min_order_notional=300.00" in caplog.text
    assert "max_single_dynamic_notional=25000.00" in caplog.text
    assert "position_already_held=False" in caplog.text
    assert "rebalance_deploy_mode=deploy" in caplog.text
    assert "ALLOCATOR_SKIP_REASON symbol=BBCP reason=no_trade_cycle_allowed" in caplog.text
    broker.get_latest_quote.assert_not_called()


def _high_conviction_allocator_config(enabled: bool = True) -> dict:
    return {
        "trading": {
            "dynamic": {
                "high_conviction_news_override": {
                    "enabled": enabled,
                    "min_catalyst_score": 8.0,
                    "min_event_score": 7.0,
                    "min_news_score": 7.0,
                    "min_relative_volume": 1.5,
                    "replacement_strength_ratio": 1.05,
                    "normal_replacement_strength_ratio": 1.2,
                    "require_positive_sentiment": True,
                }
            }
        },
        "portfolio": {"target_dynamic_pct": 0.25},
    }


def _high_conviction_candidate() -> dict:
    return {
        "symbol": "BBCP",
        "score": 9.0,
        "strength_eff": 0.86,
        "dynamic_candidate": True,
        "news_score": 9.0,
        "event_score": 8.0,
        "catalyst_score": 0.9,
        "relative_volume": 2.0,
        "volume_confirmed": True,
        "vwap_above": True,
        "source": "dynamic_universe",
    }


def test_high_conviction_rotation_relaxation_marks_eligible_candidate(caplog) -> None:
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        out = _apply_high_conviction_rotation_relaxation(
            [_high_conviction_candidate()],
            config=_high_conviction_allocator_config(enabled=True),
            ca_cfg={"replacement_strength_ratio": 1.2},
            portfolio=[],
            tracked={},
            equity=100_000.0,
            min_realloc_leg=300.0,
            allow_allocator_buys=True,
            cycle_risk_state={"daily_loss_lockout": False},
            exit_context=None,
            engine=None,
        )

    assert out[0]["high_conviction_rotation_relaxed"] is True
    assert out[0]["replacement_strength_ratio_override"] == pytest.approx(1.05)
    assert out[0]["replacement_strength_ratio_original"] == pytest.approx(1.2)
    assert "HIGH_CONVICTION_ROTATION_RELAXED symbol=BBCP old_ratio=1.200000 new_ratio=1.050000" in caplog.text


def test_high_conviction_rotation_relaxation_disabled_leaves_candidate_normal(caplog) -> None:
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        out = _apply_high_conviction_rotation_relaxation(
            [_high_conviction_candidate()],
            config=_high_conviction_allocator_config(enabled=False),
            ca_cfg={"replacement_strength_ratio": 1.2},
            portfolio=[],
            tracked={},
            equity=100_000.0,
            min_realloc_leg=300.0,
            allow_allocator_buys=True,
            cycle_risk_state={"daily_loss_lockout": False},
            exit_context=None,
            engine=None,
        )

    assert "high_conviction_rotation_relaxed" not in out[0]
    assert "HIGH_CONVICTION_ROTATION_RELAXED" not in caplog.text


def test_high_conviction_rotation_relaxation_respects_daily_lockout(caplog) -> None:
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        out = _apply_high_conviction_rotation_relaxation(
            [_high_conviction_candidate()],
            config=_high_conviction_allocator_config(enabled=True),
            ca_cfg={"replacement_strength_ratio": 1.2},
            portfolio=[],
            tracked={},
            equity=100_000.0,
            min_realloc_leg=300.0,
            allow_allocator_buys=True,
            cycle_risk_state={"daily_loss_lockout": True},
            exit_context=None,
            engine=None,
        )

    assert "high_conviction_rotation_relaxed" not in out[0]
    assert "HIGH_CONVICTION_ROTATION_REJECTED symbol=BBCP reason=daily_loss_lockout:daily_loss_lockout" in caplog.text


def test_high_conviction_rotation_relaxation_respects_dynamic_sleeve_cap(caplog) -> None:
    cfg = _high_conviction_allocator_config(enabled=True)
    cfg["portfolio"]["target_dynamic_pct"] = 0.01
    with caplog.at_level("INFO", logger="src.capital_allocator_loop"):
        out = _apply_high_conviction_rotation_relaxation(
            [_high_conviction_candidate()],
            config=cfg,
            ca_cfg={"replacement_strength_ratio": 1.2},
            portfolio=[{"symbol": "DYN", "value": 1_000.0, "score": 0.5, "dynamic_candidate": True}],
            tracked={"DYN": {"source": "dynamic_universe"}},
            equity=100_000.0,
            min_realloc_leg=300.0,
            allow_allocator_buys=True,
            cycle_risk_state={"daily_loss_lockout": False},
            exit_context=None,
            engine=None,
        )

    assert "high_conviction_rotation_relaxed" not in out[0]
    assert "HIGH_CONVICTION_ROTATION_REJECTED symbol=BBCP reason=dynamic_sleeve_cap_exceeded" in caplog.text
