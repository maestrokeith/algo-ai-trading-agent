"""
Hedge-fund-style v2 helpers (config under ``config["strategy_v2"]``).

Loaded via :func:`src.config_loader.load_app_config` when ``strategy_v2.yaml`` exists.
Portfolio loop integration is incremental; pure functions are safe to call from tests
or future orchestration.
"""
from __future__ import annotations

from .cycle import (
    EntryCycleReport,
    entry_cycle,
    evaluate_hedge,
    evaluate_hedge_place,
    evaluate_longs,
    evaluate_options,
    run_entry_cycle,
)
from .entry_signals import (
    allow_long_for_regime,
    price_breakout_last,
    rsi_wilder_last,
    should_enter_long,
)
from .hedge import (
    compute_hedge_size,
    hedge_allocation_pct,
    hedge_symbol,
    place_hedge_order,
)
from .options_alpha import check_iv_rank_proxy, options_signal, options_signal_independent
from .options_trade import trade_options
from .rebalance import RebalanceTarget, compute_targets_v2, rebalance_plan
from .regime import regime_hedge_mult_for_score, regime_long_mult_for_score
from .sizing import position_size_v2

__all__ = [
    "RebalanceTarget",
    "EntryCycleReport",
    "check_iv_rank_proxy",
    "compute_hedge_size",
    "evaluate_hedge_place",
    "hedge_allocation_pct",
    "hedge_symbol",
    "place_hedge_order",
    "entry_cycle",
    "evaluate_hedge",
    "evaluate_longs",
    "evaluate_options",
    "run_entry_cycle",
    "compute_targets_v2",
    "options_signal",
    "options_signal_independent",
    "trade_options",
    "position_size_v2",
    "price_breakout_last",
    "rebalance_plan",
    "regime_hedge_mult_for_score",
    "regime_long_mult_for_score",
    "rsi_wilder_last",
    "should_enter_long",
    "allow_long_for_regime",
]
