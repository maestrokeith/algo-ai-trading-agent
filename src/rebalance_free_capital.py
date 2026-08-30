"""Legacy compatibility wrapper for the rebalance/free-capital split.

The implementation now lives under :mod:`src.portfolio`, but this module
remains as the stable import path for tests and older callers.
"""

from __future__ import annotations

from src.portfolio.rebalance_cash import (
    emergency_bulk_trim_notional_usd,
    rfc_uses_largest_exposure_notional_trim,
    trim_fraction_by_gross_leverage,
)
from src.portfolio.rebalance_planner import (
    broker_long_shares_for_symbol,
    broker_position_market_value_usd,
    broker_position_unrealized_pnl_pct,
    get_top_n_positions,
    gross_liquidation_trim_shares,
    just_trimmed_position,
    long_stock_symbols_by_market_value_desc,
    parse_rebalance_free_capital_cfg,
    plan_bulk_notional_trims_for_free_capital,
    plan_emergency_deleverage_portfolio_pct_trims,
    plan_full_exit_weakest_for_gross_delever,
    plan_full_exit_weakest_when_stronger,
    plan_proportional_gross_delever_notional_trims,
    plan_weakest_gross_unwind_phase1,
    plan_weakest_trim_for_free_capital,
    position_has_signal_deterioration,
    rebalance_trim_fraction_for_attempt,
    symbols_ordered_for_bulk_trim_priority,
    trim_candidate_symbols_largest_exposure_notional,
)
from src.portfolio.rebalance_trims import (
    effective_allow_add_after_capital_trim,
    trim_qty_for_fraction,
)

__all__ = [
    "effective_allow_add_after_capital_trim",
    "emergency_bulk_trim_notional_usd",
    "get_top_n_positions",
    "parse_rebalance_free_capital_cfg",
    "plan_bulk_notional_trims_for_free_capital",
    "plan_emergency_deleverage_portfolio_pct_trims",
    "plan_full_exit_weakest_for_gross_delever",
    "plan_full_exit_weakest_when_stronger",
    "plan_proportional_gross_delever_notional_trims",
    "plan_weakest_gross_unwind_phase1",
    "plan_weakest_trim_for_free_capital",
    "position_has_signal_deterioration",
    "rfc_uses_largest_exposure_notional_trim",
    "trim_fraction_by_gross_leverage",
    "trim_qty_for_fraction",
        "broker_long_shares_for_symbol",
    "broker_position_market_value_usd",
    "broker_position_unrealized_pnl_pct",
    "gross_liquidation_trim_shares",
    "just_trimmed_position",
    "long_stock_symbols_by_market_value_desc",
    "rebalance_trim_fraction_for_attempt",
    "symbols_ordered_for_bulk_trim_priority",
    "trim_candidate_symbols_largest_exposure_notional",
]
