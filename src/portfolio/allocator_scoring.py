"""Scoring surface for the capital allocator split."""

from __future__ import annotations

from src.portfolio.allocator_planner import (
    allocator_book_sector_theme_pct,
    allocator_candidate_book_score,
    allocator_bullish_regime_for_defensive_drift,
    allocator_symbol_is_defensive_drift_name,
    apply_allocator_defensive_drift_scores,
    reorder_allocator_candidates_diversification,
)

__all__ = [
    "allocator_book_sector_theme_pct",
    "allocator_bullish_regime_for_defensive_drift",
    "allocator_candidate_book_score",
    "allocator_symbol_is_defensive_drift_name",
    "apply_allocator_defensive_drift_scores",
    "reorder_allocator_candidates_diversification",
]
