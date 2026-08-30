"""Legacy compatibility wrapper for the capital allocator split.

The allocator implementation now lives under :mod:`src.portfolio`, but this
module remains as the stable import path for tests and older callers.
"""

from __future__ import annotations

from src.portfolio.allocator_actions import (
    AllocatorAction,
    clip_buy_actions_to_gross_headroom_dollars,
    consolidate_allocator_actions_net_by_symbol,
    gross_book_near_effective_max_for_net_reduction,
    trim_allocator_actions_for_max_buy_to_sell_ratio,
    trim_allocator_actions_for_net_sell_gte_buy,
)
from src.portfolio.allocator_caps import (
    clip_allocator_buy_notionals_to_single_order_caps,
    effective_capital_allocator_symbol_cap_frac,
    effective_capital_allocator_symbol_cap_soft_hard,
    effective_capital_allocator_symbol_caps_by_symbol,
)
from src.portfolio.allocator_config import (
    parse_capital_allocator_cfg,
    parse_defensive_drift_cfg,
)
from src.portfolio.allocator_planner import (
    CapitalAllocator,
    collect_symbol_cap_tier_hard_fractions,
    symbol_caps_define_tier_buckets,
)
from src.portfolio.allocator_scoring import (
    allocator_book_sector_theme_pct,
    allocator_bullish_regime_for_defensive_drift,
    allocator_candidate_book_score,
    allocator_symbol_is_defensive_drift_name,
    apply_allocator_defensive_drift_scores,
    reorder_allocator_candidates_diversification,
)

__all__ = [
    "AllocatorAction",
    "CapitalAllocator",
    "allocator_book_sector_theme_pct",
    "allocator_bullish_regime_for_defensive_drift",
    "allocator_candidate_book_score",
    "allocator_symbol_is_defensive_drift_name",
    "apply_allocator_defensive_drift_scores",
    "clip_allocator_buy_notionals_to_single_order_caps",
    "clip_buy_actions_to_gross_headroom_dollars",
    "consolidate_allocator_actions_net_by_symbol",
    "collect_symbol_cap_tier_hard_fractions",
    "effective_capital_allocator_symbol_cap_frac",
    "effective_capital_allocator_symbol_cap_soft_hard",
    "effective_capital_allocator_symbol_caps_by_symbol",
    "gross_book_near_effective_max_for_net_reduction",
    "parse_capital_allocator_cfg",
    "parse_defensive_drift_cfg",
    "reorder_allocator_candidates_diversification",
    "symbol_caps_define_tier_buckets",
    "trim_allocator_actions_for_max_buy_to_sell_ratio",
    "trim_allocator_actions_for_net_sell_gte_buy",
]
