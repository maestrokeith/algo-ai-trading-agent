from __future__ import annotations

from src.allocation_config import low_regime_stock_entry_top_n
from src.execution import symbol_post_exit_cooldown_minutes


def test_low_regime_stock_entry_top_n_only_applies_up_to_threshold() -> None:
    cfg = {"execution": {"low_regime_top_n_stock_entries": 4, "low_regime_top_n_regime_score_max": 3}}
    assert low_regime_stock_entry_top_n(cfg, regime_score=2) == 4
    assert low_regime_stock_entry_top_n(cfg, regime_score=3) == 4
    assert low_regime_stock_entry_top_n(cfg, regime_score=4) == 0


def test_symbol_post_exit_cooldown_has_minimum_floor() -> None:
    cfg = {"execution": {"symbol_cooldown_minutes": 2, "min_recent_exit_reentry_minutes": 75}}
    assert symbol_post_exit_cooldown_minutes("NVDA", cfg) == 75.0
