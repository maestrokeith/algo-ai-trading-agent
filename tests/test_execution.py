"""ExecutionManager config and spread gate."""

from __future__ import annotations

import pytest

from src.execution import (
    ExecutionManager,
    OrderType,
    execution_bypass_no_sell_after_buy_cooldown,
    execution_bucket_top_signal_qualified,
    execution_rebalance_deferred_because_incoming_strong,
    limit_price_inside_spread,
    parse_no_sell_within_min_of_buy,
)
from src.strategy import ExitReason


def test_allow_fractional_reads_execution_config() -> None:
    assert ExecutionManager({"execution": {}}).allow_fractional is True


def test_allow_fractional_shares_alias() -> None:
    assert ExecutionManager({"execution": {"allow_fractional_shares": False}}).allow_fractional is False
    m = ExecutionManager(
        {
            "execution": {
                "allow_fractional": True,
                "allow_fractional_shares": False,
            }
        }
    )
    assert m.allow_fractional is True


def test_min_buying_power_probe_whole_shares_uses_full_price() -> None:
    m = ExecutionManager({"execution": {"allow_fractional": False, "min_order_notional": 200.0}})
    assert m.min_buying_power_for_equity_entry_probe(450.0) == pytest.approx(450.0)


def test_min_buying_power_probe_fractional_uses_max_of_floors() -> None:
    m = ExecutionManager(
        {
            "execution": {
                "min_order_notional": 200.0,
                "min_trade_dollars": 300.0,
            }
        }
    )
    assert m.min_buying_power_for_equity_entry_probe(500.0) == pytest.approx(300.0)
    m2 = ExecutionManager({"execution": {"min_order_notional": 200.0, "min_trade_dollars": 0.0}})
    assert m2.min_buying_power_for_equity_entry_probe(500.0) == pytest.approx(200.0)


def test_min_buying_power_probe_fractional_default_1_dollar() -> None:
    m = ExecutionManager({"execution": {}})
    assert m.min_buying_power_for_equity_entry_probe(800.0) == pytest.approx(1.0)


def test_build_order_for_entry_spread_retry_ignores_gate() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 0.7,
                "prefer_limit_orders": False,
                "retry_on_spread_fail": True,
                "retry_attempts": 2,
            }
        }
    )
    assert mgr.build_order("QQQ", "buy", 1, 100.0, 5.0) is None
    req = mgr.build_order_for_entry("QQQ", "buy", 1, 100.0, 5.0)
    assert req is not None
    assert req.quantity == 1


def test_build_order_for_entry_respects_spread_when_retry_off() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 0.7,
                "retry_on_spread_fail": False,
            }
        }
    )
    assert mgr.build_order_for_entry("QQQ", "buy", 1, 100.0, 5.0) is None


def test_block_exits_if_no_reentry_capacity_default_off() -> None:
    ex = ExecutionManager({"execution": {}})
    assert ex.block_exits_if_no_reentry_capacity is False
    assert ex.retry_on_spread_fail is False
    assert ex.spread_entry_retry_attempts == 0
    assert ExecutionManager(
        {"execution": {"block_exits_if_no_reentry_capacity": True}}
    ).block_exits_if_no_reentry_capacity is True
    assert ExecutionManager({"execution": {"allow_fractional": False}}).allow_fractional is False
    assert ExecutionManager({"execution": {"allow_fractional": True}}).allow_fractional is True


def test_min_trade_dollars_default_off() -> None:
    assert ExecutionManager({"execution": {}}).min_trade_dollars == pytest.approx(0.0)


def test_min_trade_dollars_blocks_small_explicit_notional_buy() -> None:
    mgr = ExecutionManager(
        {"execution": {"max_spread_pct": 5.0, "min_trade_dollars": 300.0, "prefer_limit_orders": False}}
    )
    assert mgr.build_order("SPY", "buy", 0, 100.0, 0.05, notional=100.0) is None
    req = mgr.build_order("SPY", "buy", 0, 100.0, 0.05, notional=400.0)
    assert req is not None
    assert req.notional == pytest.approx(400.0)


def test_min_trade_dollars_blocks_small_derived_notional_buy() -> None:
    mgr = ExecutionManager(
        {"execution": {"max_spread_pct": 5.0, "min_trade_dollars": 300.0, "prefer_limit_orders": False}}
    )
    assert mgr.build_order("SPY", "buy", 1, 100.0, 0.05) is None
    assert "below min_trade_dollars" in (mgr.last_order_build_reject_reason or "")
    req = mgr.build_order("SPY", "buy", 4, 100.0, 0.05)
    assert req is not None
    assert req.notional == pytest.approx(400.0)


def test_build_order_for_entry_fractional_clips_to_min_trade_dollars() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "allow_fractional": True,
                "max_spread_pct": 5.0,
                "min_trade_dollars": 500.0,
                "prefer_limit_orders": False,
            }
        }
    )

    req = mgr.build_order_for_entry("AAPL", "buy", 2, 200.0, 0.05)

    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.notional == pytest.approx(500.0)


def test_build_order_for_entry_does_not_retry_non_spread_reject() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "prefer_limit_orders": False,
                "retry_on_spread_fail": True,
                "retry_attempts": 2,
            }
        }
    )

    assert mgr.build_order_for_entry("AAPL", "buy", 0, 100.0, 0.05) is None
    assert mgr.last_order_build_reject_reason == "quantity <= 0"


def test_build_order_equity_buy_whole_shares_when_allow_fractional_false() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "allow_fractional": False,
                "prefer_limit_orders": True,
            }
        }
    )
    req = mgr.build_order("SPY", "buy", 10, 400.0, 0.1)
    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.quantity == 10
    assert req.notional is None


def test_build_order_explicit_notional_whole_shares_when_allow_fractional_false() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "allow_fractional": False,
            }
        }
    )
    req = mgr.build_order("SPY", "buy", 0, 100.0, 0.05, notional=250.0)
    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.quantity == 2
    assert req.notional is None


def test_max_spread_pct_sets_trade_cap() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct": 0.7}})
    assert mgr.max_spread_pct_to_trade == 0.7
    ok, _ = mgr.can_trade_spread(0.6)
    assert ok
    ok, _ = mgr.can_trade_spread(0.8)
    assert not ok


def test_max_spread_pct_to_trade_when_max_spread_pct_absent() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct_to_trade": 5.0}})
    assert mgr.max_spread_pct_to_trade == 5.0


def test_default_max_spread_when_both_absent() -> None:
    mgr = ExecutionManager({"execution": {}})
    assert mgr.max_spread_pct_to_trade == 1.0


def test_tiered_max_spread_by_symbol() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": {
                    "large_caps": 1.0,
                    "mid_caps": 1.5,
                    "small_caps": 2.0,
                },
                "large_cap_symbols": ["QQQ"],
                "mid_cap_symbols": ["IWM"],
            }
        }
    )
    assert mgr.max_spread_pct_for_symbol("QQQ") == 1.0
    assert mgr.max_spread_pct_for_symbol("IWM") == 1.5
    assert mgr.max_spread_pct_for_symbol("OTH") == 2.0
    assert mgr.can_trade_spread(1.1, "QQQ")[0] is False
    assert mgr.can_trade_spread(1.1, "IWM")[0] is True
    assert mgr.can_trade_spread(2.1, "OTH")[0] is False
    assert mgr.build_order("OTH", "buy", 1, 10.0, 1.9) is not None


def test_tiered_without_symbol_lists_uses_large_cap() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": {
                    "large_caps": 1.0,
                    "mid_caps": 1.5,
                    "small_caps": 2.0,
                },
            }
        }
    )
    assert mgr.max_spread_pct_for_symbol("ANY") == 1.0


def test_tiered_large_and_volatile_only_no_lists_uses_volatile_cap() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": {"large_caps": 1.0, "volatile": 2.5},
            }
        }
    )
    assert mgr.max_spread_pct_for_symbol("ANY") == pytest.approx(2.5)


def test_skip_if_spread_above_pct_tightens_cap() -> None:
    mgr = ExecutionManager(
        {"execution": {"max_spread_pct": 4.0, "skip_if_spread_above_pct": 0.5}}
    )
    assert mgr.max_spread_pct_for_symbol("XYZ") == pytest.approx(0.5)
    ok, _ = mgr.can_trade_spread(0.5, "XYZ")
    assert ok
    ok2, _ = mgr.can_trade_spread(0.51, "XYZ")
    assert ok2 is False


def test_dynamic_universe_symbols_use_dynamic_execution_spread_cap() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": {
                    "large_caps": 1.0,
                    "small_caps": 2.0,
                },
                "large_cap_symbols": ["SPY"],
            },
            "dynamic_universe": {"execution_max_spread_pct": 8.0},
        }
    )
    mgr.set_dynamic_universe_symbols(["SMCI"])

    assert mgr.max_spread_pct_for_symbol("SPY") == pytest.approx(1.0)
    assert mgr.max_spread_pct_for_symbol("SMCI") == pytest.approx(8.0)
    assert mgr.can_trade_spread(7.5, "SMCI")[0] is True
    assert mgr.can_trade_spread(8.5, "SMCI")[0] is False
    assert mgr.can_trade_spread(1.5, "SPY")[0] is False


def test_skip_if_spread_above_pct_min_with_tiers() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": {
                    "large_caps": 1.0,
                    "mid_caps": 1.5,
                    "small_caps": 2.0,
                },
                "large_cap_symbols": ["QQQ"],
                "skip_if_spread_above_pct": 0.5,
            }
        }
    )
    assert mgr.max_spread_pct_for_symbol("QQQ") == pytest.approx(0.5)
    assert mgr.max_spread_pct_for_symbol("OTH") == pytest.approx(0.5)


def test_can_trade_spread_ignore_gate() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct": 0.7}})
    ok, _ = mgr.can_trade_spread(5.0, "QQQ", ignore_spread_gate=True)
    assert ok


def test_limit_price_inside_spread_buy_explicit_nbbo() -> None:
    p = limit_price_inside_spread(
        "buy",
        bid=100.0,
        ask=100.2,
        mid_price=100.1,
        spread_pct=0.2,
        inside_fraction=0.25,
    )
    assert p == pytest.approx(100.0 + 0.25 * 0.2)


def test_limit_price_inside_spread_sell_explicit_nbbo() -> None:
    p = limit_price_inside_spread(
        "sell",
        bid=50.0,
        ask=50.4,
        mid_price=50.2,
        spread_pct=0.79,
        inside_fraction=0.25,
    )
    assert p == pytest.approx(50.4 - 0.25 * 0.4)


def test_build_order_inside_spread_from_mid_only() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "limit_price_mode": "inside_spread",
                "inside_spread_fraction": 0.25,
                "limit_price_round_decimals": 2,
                "prefer_limit_orders": True,
            }
        }
    )
    req = mgr.build_order("SPY", "buy", 10, 400.0, 0.1)
    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.notional == pytest.approx(4000.0)
    assert req.limit_price is None


def test_build_order_mid_offset_ticks_legacy() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "limit_price_mode": "mid_offset_ticks",
                "limit_order_offset_ticks": 1,
                "limit_price_round_decimals": 2,
                "prefer_limit_orders": True,
            }
        }
    )
    req = mgr.build_order("SPY", "buy", 1, 100.0, 0.05, tick_size=0.01)
    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.notional == pytest.approx(100.0)


def test_build_order_notional_forces_market_even_when_limit_preferred() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "prefer_limit_orders": True,
                "limit_price_mode": "inside_spread",
            }
        }
    )
    req = mgr.build_order("QQQ", "buy", 5, 400.0, 0.05, notional=1000.0)
    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.notional == pytest.approx(1000.0)
    assert req.limit_price is None


def test_build_order_from_dict_notional() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct": 5.0, "prefer_limit_orders": False}})
    req = mgr.build_order_from_dict(
        {"symbol": "SPY", "notional": 1000, "side": "buy"},
        mid_price=450.0,
        spread_pct=0.02,
    )
    assert req is not None
    assert req.symbol == "SPY"
    assert req.side == "buy"
    assert req.notional == pytest.approx(1000.0)
    assert req.order_type == OrderType.MARKET


def test_build_order_from_dict_logs_reject_reason_with_route(caplog: pytest.LogCaptureFixture) -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "min_trade_dollars": 1500.0,
                "prefer_limit_orders": False,
            }
        }
    )

    with caplog.at_level("INFO", logger="src.execution"):
        req = mgr.build_order_from_dict(
            {
                "symbol": "SMH",
                "notional": 1200.0,
                "side": "buy",
                "route": "core_rebuild",
                "source": "core_rebuild",
            },
            mid_price=250.0,
            spread_pct=0.02,
        )

    assert req is None
    assert mgr.last_order_build_reject_reason == "notional $1200.00 below min_trade_dollars $1500.00"
    assert (
        "ORDER_BUILD_REJECT symbol=SMH reason=notional $1200.00 below min_trade_dollars $1500.00 "
        "route=core_rebuild source=core_rebuild notional=1200.00 qty=0.0000"
    ) in caplog.text


def test_build_order_from_dict_qty() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct": 5.0, "prefer_limit_orders": False}})
    req = mgr.build_order_from_dict(
        {"symbol": "SPY", "qty": 3, "side": "buy"},
        mid_price=450.0,
        spread_pct=0.02,
    )
    assert req is not None
    assert req.quantity == 3
    assert req.notional == pytest.approx(1350.0)
    assert req.order_type == OrderType.MARKET


def test_build_order_tight_spread_uses_market_when_threshold_configured() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "market_order_if_spread_below_pct": 0.5,
                "prefer_limit_orders": True,
            }
        }
    )
    req = mgr.build_order("SPY", "buy", 10, 100.0, 0.1)
    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.notional == pytest.approx(1000.0)


def test_build_order_wide_spread_uses_limit_at_mid_when_threshold_configured() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "market_order_if_spread_below_pct": 0.5,
                "prefer_limit_orders": True,
                "limit_price_round_decimals": 2,
            }
        }
    )
    req = mgr.build_order("SPY", "buy", 10, 100.0, 0.6)
    assert req is not None
    assert req.order_type == OrderType.LIMIT
    assert req.limit_price == pytest.approx(100.0)
    assert req.notional is None
    assert req.quantity == 10


def test_build_order_equity_sell_limit_at_mid_when_spread_wide() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "market_order_if_spread_below_pct": 0.1,
                "prefer_limit_orders": False,
            }
        }
    )
    req = mgr.build_order("MSFT", "sell", 7, 425.23, 0.5)
    assert req is not None
    assert req.order_type == OrderType.LIMIT
    assert req.limit_price == pytest.approx(425.23)
    assert req.quantity == 7
    assert req.notional is None


def test_equity_sell_partial_bumped_to_min_pct_and_notional() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "prefer_limit_orders": False,
                "min_sell_pct_of_position": 0.2,
                "min_trade_value": 250,
            }
        }
    )
    # 100 sh @ $100: 20% = 20 sh; $250 min → ceil(2.5)=3; floor = 20
    req = mgr.build_order("SPY", "sell", 1, 100.0, 0.05, position_qty=100)
    assert req is not None
    assert req.quantity == 20


def test_equity_sell_full_exit_unchanged_when_below_floor_pct() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "prefer_limit_orders": False,
                "min_sell_pct_of_position": 0.2,
                "min_trade_value": 250,
            }
        }
    )
    req = mgr.build_order("SPY", "sell", 7, 100.0, 0.05, position_qty=7)
    assert req is not None
    assert req.quantity == 7


def test_equity_sell_final_chunk_includes_fractional_tail() -> None:
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "prefer_limit_orders": False,
                "min_sell_pct_of_position": 0.2,
                "min_trade_value": 250,
            }
        }
    )
    req = mgr.build_order("SPY", "sell", 7, 100.0, 0.05, position_qty=7.4)
    assert req is not None
    assert req.quantity == pytest.approx(7.4)
    assert req.notional is None


def test_equity_sell_sub_one_share_position_allows_fractional_liquidation() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct": 5.0, "prefer_limit_orders": False}})
    req = mgr.build_order("SPY", "sell", 0.4, 100.0, 0.05, position_qty=0.4)
    assert req is not None
    assert req.quantity == pytest.approx(0.4)
    assert req.notional is None


def test_equity_sell_partial_keeps_whole_shares_when_remainder_at_least_one() -> None:
    mgr = ExecutionManager({"execution": {"max_spread_pct": 5.0, "prefer_limit_orders": False}})
    req = mgr.build_order("SPY", "sell", 5.5, 100.0, 0.05, position_qty=7.4)
    assert req is not None
    assert req.quantity == pytest.approx(5.0)


def test_build_order_equity_sell_uses_share_qty_not_notional() -> None:
    """Whole-share exits must not use notional (avoids fractional implied qty vs Alpaca position)."""
    mgr = ExecutionManager({"execution": {"max_spread_pct": 5.0, "prefer_limit_orders": False}})
    req = mgr.build_order("MSFT", "sell", 7, 425.23, 0.05)
    assert req is not None
    assert req.order_type == OrderType.MARKET
    assert req.quantity == 7
    assert req.notional is None


def test_build_order_occ_option_uses_limit_when_preferred() -> None:
    """OCC symbols are not converted to equity-style notional market orders."""
    occ = "AAPL240315C00185000"
    mgr = ExecutionManager(
        {
            "execution": {
                "max_spread_pct": 5.0,
                "prefer_limit_orders": True,
                "limit_price_mode": "inside_spread",
            }
        }
    )
    req = mgr.build_order(occ, "buy", 2, 5.0, 0.5)
    assert req is not None
    assert req.order_type == OrderType.LIMIT
    assert req.quantity == 2
    assert req.notional is None
    assert req.limit_price is not None


def test_partial_exit_sell_quantity_zero_when_flat() -> None:
    mgr = ExecutionManager({"execution": {}})
    assert (
        mgr.partial_exit_sell_quantity(
            tracker_qty=100,
            broker_position={"symbol": "SPY", "unrealized_plpc": 0.01},
        )
        == 0
    )


def test_partial_exit_sell_quantity_thirty_percent() -> None:
    mgr = ExecutionManager({"execution": {}})
    q = mgr.partial_exit_sell_quantity(
        tracker_qty=10,
        broker_position={"symbol": "QQQ", "unrealized_plpc": 0.03},
    )
    assert q == 3


def test_partial_exit_sell_quantity_caps_at_position() -> None:
    mgr = ExecutionManager({"execution": {}})
    q = mgr.partial_exit_sell_quantity(
        tracker_qty=2,
        broker_position={"unrealized_plpc": 0.05},
    )
    assert q == 1


def test_partial_exit_sell_quantity_respects_partial_trim_trigger_pct() -> None:
    mgr = ExecutionManager(
        {
            "execution": {},
            "strategy": {
                "exits": {
                    "partial_trim_trigger_pct": 2.0,
                    "trim_fraction": 0.25,
                }
            },
        }
    )
    assert (
        mgr.partial_exit_sell_quantity(
            tracker_qty=100,
            broker_position={"unrealized_plpc": 0.015},
        )
        == 0
    )
    q = mgr.partial_exit_sell_quantity(
        tracker_qty=100,
        broker_position={"unrealized_plpc": 0.025},
    )
    assert q == 25


def test_partial_exit_sell_quantity_disabled_below_gross_threshold() -> None:
    mgr = ExecutionManager(
        {
            "execution": {},
            "strategy": {
                "exits": {
                    "partial_trim_trigger_pct": 2.0,
                    "trim_fraction": 0.25,
                    "disable_partial_trim_below_gross_pct": 0.85,
                }
            },
        }
    )
    assert (
        mgr.partial_exit_sell_quantity(
            tracker_qty=100,
            broker_position={"unrealized_plpc": 0.05},
            current_gross_pct=84.0,
        )
        == 0
    )
    assert (
        mgr.partial_exit_sell_quantity(
            tracker_qty=100,
            broker_position={"unrealized_plpc": 0.05},
            current_gross_pct=85.0,
        )
        == 25
    )


def test_execution_bucket_top_signal_qualified_top_fraction() -> None:
    cfg = {
        "execution": {
            "allow_bucket_override_for_top_signals": True,
            "top_signal_percentile": 0.2,
        }
    }
    c = [0.5, 0.6, 0.7, 0.8, 0.9]
    assert execution_bucket_top_signal_qualified(cfg, strength=0.9, strength_cohort=c) is True
    assert execution_bucket_top_signal_qualified(cfg, strength=0.5, strength_cohort=c) is False


def test_execution_bucket_top_signal_qualified_requires_cohort_size() -> None:
    cfg = {
        "execution": {
            "allow_bucket_override_for_top_signals": True,
            "top_signal_percentile": 0.2,
        }
    }
    assert execution_bucket_top_signal_qualified(cfg, strength=0.99, strength_cohort=[0.99]) is False


def test_execution_bucket_top_signal_qualified_flag_off() -> None:
    assert (
        execution_bucket_top_signal_qualified(
            {"execution": {"allow_bucket_override_for_top_signals": False}},
            strength=0.9,
            strength_cohort=[0.1, 0.9],
        )
        is False
    )


def test_rebalance_deferred_strong_cohort_top_bucket() -> None:
    cfg = {
        "execution": {
            "disable_rebalance_if_strong_signals": True,
            "allow_bucket_override_for_top_signals": True,
            "top_signal_percentile": 0.2,
        }
    }
    c = [0.5, 0.6, 0.7, 0.8, 0.9]
    assert (
        execution_rebalance_deferred_because_incoming_strong(
            cfg, incoming_strength=0.9, strength_cohort=c
        )
        is True
    )
    assert (
        execution_rebalance_deferred_because_incoming_strong(
            cfg, incoming_strength=0.5, strength_cohort=c
        )
        is False
    )


def test_rebalance_deferred_strong_single_name_uses_min() -> None:
    cfg = {
        "execution": {
            "disable_rebalance_if_strong_signals": True,
            "rebalance_incoming_strength_block_min": 0.85,
        }
    }
    assert (
        execution_rebalance_deferred_because_incoming_strong(
            cfg, incoming_strength=0.9, strength_cohort=[0.9]
        )
        is True
    )
    assert (
        execution_rebalance_deferred_because_incoming_strong(
            cfg, incoming_strength=0.7, strength_cohort=None
        )
        is False
    )


def test_rebalance_deferred_flag_off() -> None:
    assert (
        execution_rebalance_deferred_because_incoming_strong(
            {"execution": {"disable_rebalance_if_strong_signals": False}},
            incoming_strength=0.99,
            strength_cohort=None,
        )
        is False
    )


def test_parse_no_sell_within_min_of_buy() -> None:
    assert parse_no_sell_within_min_of_buy({}) == pytest.approx(0.0)
    assert parse_no_sell_within_min_of_buy({"execution": {}}) == pytest.approx(0.0)
    assert parse_no_sell_within_min_of_buy(
        {"execution": {"no_sell_within_min_of_buy": 30}}
    ) == pytest.approx(30.0)


def test_bypass_no_sell_after_buy() -> None:
    assert execution_bypass_no_sell_after_buy_cooldown(ExitReason.STOP_LOSS) is True
    assert execution_bypass_no_sell_after_buy_cooldown(ExitReason.RISK_CAP_REBALANCE) is True
    assert execution_bypass_no_sell_after_buy_cooldown(ExitReason.TAKE_PROFIT) is False
