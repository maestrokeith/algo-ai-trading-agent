"""Action post-processing surface for the capital allocator split."""

from __future__ import annotations

from src.portfolio.allocator_planner import (
    AllocatorAction,
    clip_buy_actions_to_gross_headroom_dollars,
    consolidate_allocator_actions_net_by_symbol,
    gross_book_near_effective_max_for_net_reduction,
    trim_allocator_actions_for_max_buy_to_sell_ratio,
    trim_allocator_actions_for_net_sell_gte_buy,
)

__all__ = [
    "AllocatorAction",
    "clip_buy_actions_to_gross_headroom_dollars",
    "consolidate_allocator_actions_net_by_symbol",
    "gross_book_near_effective_max_for_net_reduction",
    "trim_allocator_actions_for_max_buy_to_sell_ratio",
    "trim_allocator_actions_for_net_sell_gte_buy",
]
