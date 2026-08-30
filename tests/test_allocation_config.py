from __future__ import annotations

from src.allocation_config import (
    effective_allocate_top_n,
    effective_ranked_signals_cap,
    parse_allocation_config,
)


def test_effective_allocate_top_n_int() -> None:
    assert effective_allocate_top_n(7) == 7
    assert effective_allocate_top_n("8") == 8


def test_effective_allocate_top_n_range_string() -> None:
    assert effective_allocate_top_n("5-8") == 6
    assert effective_allocate_top_n("5 – 8") == 6
    assert effective_allocate_top_n("3-5") == 4


def test_effective_allocate_top_n_min_max() -> None:
    assert effective_allocate_top_n(None, lo=5, hi=8) == 6


def test_parse_allocation_defaults() -> None:
    d = parse_allocation_config({})
    assert d["rank_by_signal_strength"] is False
    assert d["allocate_top_n"] == 6
    assert d["rank_top_k_by"] == "strength_eff"


def test_parse_allocation_rank_top_k_by() -> None:
    d = parse_allocation_config(
        {
            "allocation": {
                "rank_by_signal_strength": True,
                "rank_top_k_by": "momentum_rs_volume",
                "allocate_top_n": 5,
            }
        }
    )
    assert d["rank_top_k_by"] == "momentum_rs_volume"


def test_parse_allocation_yml_style() -> None:
    d = parse_allocation_config(
        {
            "allocation": {
                "rank_by_signal_strength": True,
                "allocate_top_n": "5-8",
            }
        }
    )
    assert d["rank_by_signal_strength"] is True
    assert d["allocate_top_n"] == 6


def test_effective_ranked_signals_cap_uses_top_k_when_strength_rank() -> None:
    cfg = {
        "allocation": {
            "rank_by_signal_strength": True,
            "allocate_top_n": "3-5",
        },
        "portfolio": {"signal_ranking": {"max_signals_per_loop": 99}},
    }
    assert effective_ranked_signals_cap(cfg) == 4


def test_effective_ranked_signals_cap_falls_back_when_no_strength_rank() -> None:
    cfg = {
        "allocation": {"rank_by_signal_strength": False},
        "portfolio": {"signal_ranking": {"max_signals_per_loop": 5}},
    }
    assert effective_ranked_signals_cap(cfg) == 5
