"""Tests for top-level ``alpha:`` (ranking + composite weights)."""

from __future__ import annotations

import pytest

from src.alpha_config import (
    alpha_cap_ranked_take,
    alpha_rank_candidates,
    alpha_selection_method,
    alpha_signal_ranking_mode_override,
    effective_composite_weights,
    parse_alpha_scoring_weights,
)
from src.risk_limits import risk_max_new_positions_per_cycle
from src.signal_ranking import SIGNAL_RANKING_MODE_COMPOSITE


def test_parse_alpha_scoring_maps_and_renormalizes() -> None:
    cfg = {
        "alpha": {
            "scoring": {
                "weight_trend": 0.3,
                "weight_momentum": 0.3,
                "weight_pullback": 0.2,
                "weight_volatility": 0.2,
            }
        }
    }
    w = parse_alpha_scoring_weights(cfg)
    assert w is not None
    assert w["trend_strength"] == pytest.approx(0.3)
    assert w["momentum"] == pytest.approx(0.3)
    assert w["relative_strength"] == pytest.approx(0.2)
    assert w["volatility_expansion"] == pytest.approx(0.2)


def test_effective_composite_weights_prefers_alpha_over_portfolio() -> None:
    cfg = {
        "alpha": {
            "scoring": {
                "weight_trend": 0.5,
                "weight_momentum": 0.5,
                "weight_pullback": 0.0,
                "weight_volatility": 0.0,
            }
        },
        "portfolio": {
            "signal_ranking": {
                "composite_weights": {
                    "trend_strength": 0.1,
                    "momentum": 0.9,
                    "volatility_expansion": 0.0,
                    "relative_strength": 0.0,
                }
            }
        },
    }
    w = effective_composite_weights(cfg)
    assert w["trend_strength"] == pytest.approx(0.5)
    assert w["momentum"] == pytest.approx(0.5)


def test_alpha_rank_candidates_defaults() -> None:
    assert alpha_rank_candidates({}) is True
    assert alpha_rank_candidates({"alpha": {"rank_candidates": False}}) is False


def test_alpha_signal_ranking_mode_override_top_n() -> None:
    cfg = {
        "alpha": {
            "rank_candidates": True,
            "selection_method": "top_n",
            "scoring": {"weight_trend": 1.0, "weight_momentum": 0, "weight_pullback": 0, "weight_volatility": 0},
        }
    }
    assert alpha_signal_ranking_mode_override(cfg) == SIGNAL_RANKING_MODE_COMPOSITE


def test_alpha_mode_override_off_when_allocator_top_k() -> None:
    cfg = {
        "allocation": {"rank_by_signal_strength": True, "allocate_top_n": 3},
        "alpha": {
            "rank_candidates": True,
            "selection_method": "top_n",
            "scoring": {
                "weight_trend": 0.3,
                "weight_momentum": 0.3,
                "weight_pullback": 0.2,
                "weight_volatility": 0.2,
            },
        },
    }
    assert alpha_signal_ranking_mode_override(cfg) is None


def test_alpha_selection_method_default() -> None:
    assert alpha_selection_method({}) == "top_n"


def test_alpha_cap_ranked_take_defaults_to_requested() -> None:
    assert alpha_cap_ranked_take({}, 5) == 5
    assert alpha_cap_ranked_take({"alpha": {}}, 3) == 3


def test_alpha_cap_ranked_take_select_top_k() -> None:
    cfg = {"alpha": {"select_top_k": 1}}
    assert alpha_cap_ranked_take(cfg, 5) == 1
    assert alpha_cap_ranked_take(cfg, 0) == 0


def test_alpha_cap_ranked_take_only_best() -> None:
    cfg = {"alpha": {"only_best": True}}
    assert alpha_cap_ranked_take(cfg, 10) == 1


def test_alpha_cap_ranked_take_clamps_to_requested() -> None:
    assert alpha_cap_ranked_take({"alpha": {"select_top_k": 99}}, 2) == 2


def test_risk_max_new_prefers_alpha() -> None:
    cfg = {
        "alpha": {"max_new_positions_per_cycle": 1},
        "risk": {"max_new_positions_per_cycle": 5},
    }
    assert risk_max_new_positions_per_cycle(cfg) == 1
