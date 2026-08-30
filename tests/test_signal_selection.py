"""Tests for :mod:`src.signal_selection`."""

from __future__ import annotations

import pytest

from src.signal_ranking import SIGNAL_RANKING_MODE_STRENGTH
from src.signal_selection import (
    get_valid_signals,
    rank_all_by_mode,
    row_numeric_score,
    select_top_signals,
)


def test_get_valid_signals_filters_none() -> None:
    assert get_valid_signals([{"sym_u": "A"}, None]) == [{"sym_u": "A"}]


def test_select_top_signals_k_zero_empty() -> None:
    assert select_top_signals([{"sym_u": "X"}], 0) == []


def test_rank_all_then_top_matches_strongest_first() -> None:
    rows = [
        {"sym_u": "LOW", "strength_eff": 0.3},
        {"sym_u": "HIGH", "strength_eff": 0.95},
    ]
    full = rank_all_by_mode(
        rows,
        sector_etfs=frozenset(),
        ranking_mode=SIGNAL_RANKING_MODE_STRENGTH,
    )
    assert [r["sym_u"] for r in full] == ["HIGH", "LOW"]
    top1 = select_top_signals(full, 1)
    assert top1[0]["sym_u"] == "HIGH"


def test_row_numeric_score_strength_mode() -> None:
    r = {"sym_u": "Z", "strength_eff": 0.77}
    assert row_numeric_score(r, ranking_mode=SIGNAL_RANKING_MODE_STRENGTH) == pytest.approx(
        0.77
    )
