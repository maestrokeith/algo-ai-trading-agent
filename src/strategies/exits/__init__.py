"""Split exit surfaces for live strategy workflows.

These modules provide cleaner import boundaries while ``src/live/exits.py``
remains the compatibility entry point during the migration.
"""

from src.strategies.exits.context import LiveExitContext, reentry_block_allows_despite_flag
from src.strategies.exits.option_exit import manage_option_position
from src.strategies.exits.profit_protection import (
    do_not_sell_winners_early_blocks,
    equity_long_trend_structure_still_strong,
    equity_unrealized_pnl_percent_points,
    exit_trim_suppressed_trend_still_strong,
)
from src.strategies.exits.stock_exit import manage_stock_position

__all__ = [
    "LiveExitContext",
    "do_not_sell_winners_early_blocks",
    "equity_long_trend_structure_still_strong",
    "equity_unrealized_pnl_percent_points",
    "exit_trim_suppressed_trend_still_strong",
    "manage_option_position",
    "manage_stock_position",
    "reentry_block_allows_despite_flag",
]

