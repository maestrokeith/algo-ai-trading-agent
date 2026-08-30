"""Compatibility wrapper for live exit orchestration.

The implementation has been split into ``src/strategies/exits/`` surfaces.
Keep importing from this module for now if you need the legacy path while the
rest of the codebase migrates.
"""

from __future__ import annotations

from src.strategies.exits.context import (
    LiveExitContext,
    reentry_block_allows_despite_flag,
    _LAST_EQUITY_SELL_UTC,
)
from src.strategies.exits.option_exit import manage_option_position
from src.strategies.exits.profit_protection import (
    do_not_sell_winners_early_blocks,
    equity_long_trend_structure_still_strong,
    equity_unrealized_pnl_percent_points,
    exit_trim_suppressed_trend_still_strong,
)
from src.strategies.exits.stock_exit import manage_stock_position
from src.strategies.exits.trailing import (
    bump_high_price,
    load_smart_exit_state_from_row,
    process_smart_exit,
    smart_exit_state_to_json,
    smart_trailing_cfg_for_process,
)

_reentry_block_allows_despite_flag = reentry_block_allows_despite_flag
_equity_unrealized_pnl_percent_points = equity_unrealized_pnl_percent_points

__all__ = [
    "LiveExitContext",
    "bump_high_price",
    "do_not_sell_winners_early_blocks",
    "equity_long_trend_structure_still_strong",
    "equity_unrealized_pnl_percent_points",
    "exit_trim_suppressed_trend_still_strong",
    "load_smart_exit_state_from_row",
    "manage_option_position",
    "manage_stock_position",
    "process_smart_exit",
    "reentry_block_allows_despite_flag",
    "smart_exit_state_to_json",
    "smart_trailing_cfg_for_process",
    "_LAST_EQUITY_SELL_UTC",
]
