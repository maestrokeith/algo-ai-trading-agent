from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.adaptive import (
    adaptive_bump_streak,
    adaptive_bucket_cap_mult,
    adaptive_effective_max_total_exposure,
    adaptive_strong_signal_for_cap_override,
    cap_relax_factor_effective,
    is_adaptive_cap_block_reason,
)


def _dec(allowed: bool, reason: str | None) -> SimpleNamespace:
    return SimpleNamespace(allowed=allowed, reason=reason)


def test_is_adaptive_cap_block_reason_true_for_caps_and_exposure() -> None:
    assert is_adaptive_cap_block_reason("symbol cap blocks entry")
    assert is_adaptive_cap_block_reason("spread 0.5% exceeds max")
    assert is_adaptive_cap_block_reason("gross exposure would exceed limit")


def test_is_adaptive_cap_block_reason_false_for_cooldowns_and_universe() -> None:
    assert not is_adaptive_cap_block_reason("per-symbol buy cooldown")
    assert not is_adaptive_cap_block_reason("not in universe")
    assert not is_adaptive_cap_block_reason("no entry signal for long")


def test_cap_relax_factor_off_or_below_streak() -> None:
    cfg: dict = {"adaptive": {"relax_caps_if_entry_blocked": False, "relax_factor": 1.2}}
    assert cap_relax_factor_effective(config=cfg, cap_block_streak=5) == 1.0

    cfg2: dict = {
        "adaptive": {
            "relax_caps_if_entry_blocked": True,
            "relax_factor": 1.2,
            "min_consecutive_cap_blocks": 3,
        }
    }
    assert cap_relax_factor_effective(config=cfg2, cap_block_streak=2) == 1.0
    assert cap_relax_factor_effective(config=cfg2, cap_block_streak=3) == 1.2


def test_cap_relax_clamped_below_1() -> None:
    cfg: dict = {
        "adaptive": {
            "relax_caps_if_entry_blocked": True,
            "relax_factor": 0.5,
            "min_consecutive_cap_blocks": 1,
        }
    }
    assert cap_relax_factor_effective(config=cfg, cap_block_streak=1) == 1.0


def test_adaptive_bump_streak_increments_on_cap_reason() -> None:
    cfg: dict = {
        "adaptive": {
            "relax_caps_if_entry_blocked": True,
            "relax_factor": 1.2,
        }
    }
    d = _dec(False, "gross book exposure cap")
    assert adaptive_bump_streak(cfg, 0, d) == 1
    assert adaptive_bump_streak(cfg, 1, d) == 2


def test_adaptive_bump_streak_resets_on_allow_or_non_cap() -> None:
    cfg: dict = {"adaptive": {"relax_caps_if_entry_blocked": True}}
    assert adaptive_bump_streak(cfg, 2, _dec(True, "ok")) == 0
    assert adaptive_bump_streak(cfg, 2, _dec(False, "per-symbol buy cooldown")) == 0
    assert adaptive_bump_streak(cfg, 0, _dec(True, "ok")) == 0


def test_adaptive_bump_streak_disabled_returns_zero() -> None:
    cfg: dict = {"adaptive": {"relax_caps_if_entry_blocked": False}}
    assert (
        adaptive_bump_streak(cfg, 3, _dec(False, "symbol cap")) == 0
    )


def test_adaptive_bump_streak_none_decision_unchanged() -> None:
    cfg: dict = {"adaptive": {"relax_caps_if_entry_blocked": True}}
    assert adaptive_bump_streak(cfg, 2, None) == 2


def test_strong_signal_cap_override_multiplies_without_streak() -> None:
    cfg: dict = {
        "adaptive": {
            "relax_caps_if_entry_blocked": False,
            "allow_cap_override_on_strong_signal": True,
            "override_multiplier": 1.2,
        }
    }
    assert (
        cap_relax_factor_effective(
            config=cfg,
            cap_block_streak=0,
            entry_strength=0.9,
            symbol_upper="NVDA",
        )
        == 1.2
    )
    assert (
        cap_relax_factor_effective(
            config=cfg,
            cap_block_streak=0,
            entry_strength=0.5,
            symbol_upper="NVDA",
        )
        == 1.0
    )


def test_strong_signal_stacks_with_streak_relax() -> None:
    cfg: dict = {
        "adaptive": {
            "relax_caps_if_entry_blocked": True,
            "relax_factor": 1.2,
            "min_consecutive_cap_blocks": 2,
            "allow_cap_override_on_strong_signal": True,
            "override_multiplier": 1.2,
        }
    }
    assert cap_relax_factor_effective(
        config=cfg, cap_block_streak=2, entry_strength=0.95, symbol_upper="SPY"
    ) == pytest.approx(1.44)


def test_cap_override_respects_relief_symbol_list() -> None:
    cfg: dict = {
        "adaptive": {
            "allow_cap_override_on_strong_signal": True,
            "override_multiplier": 1.2,
            "cap_override_relief_symbols": ["NVDA"],
        }
    }
    assert adaptive_strong_signal_for_cap_override(
        cfg, entry_strength=0.9, symbol_upper="NVDA"
    )
    assert not adaptive_strong_signal_for_cap_override(
        cfg, entry_strength=0.9, symbol_upper="SPY"
    )


def test_adaptive_bucket_cap_mult_uses_table_neutral() -> None:
    cfg: dict = {
        "adaptive": {
            "bucket_cap_multiplier": {
                "neutral": 1.2,
                "bullish": 1.5,
                "bearish": 0.8,
            }
        }
    }
    assert adaptive_bucket_cap_mult(
        cfg, regime_condition="neutral", regime_score=None
    ) == pytest.approx(1.2)
    assert adaptive_bucket_cap_mult(cfg, regime_score=3) == pytest.approx(1.2)


def test_adaptive_bucket_cap_mult_defensive_uses_bearish_key() -> None:
    cfg: dict = {"adaptive": {"bucket_cap_multiplier": {"bearish": 0.8, "defensive": 0.5}}}
    assert adaptive_bucket_cap_mult(
        cfg, regime_condition="defensive", regime_score=None
    ) == pytest.approx(0.5)
    del cfg["adaptive"]["bucket_cap_multiplier"]["defensive"]
    assert adaptive_bucket_cap_mult(
        cfg, regime_condition="defensive", regime_score=None
    ) == pytest.approx(0.8)
    assert adaptive_bucket_cap_mult(cfg, regime_score=0) == pytest.approx(0.8)


def test_adaptive_bucket_cap_mult_omitted_returns_one() -> None:
    assert adaptive_bucket_cap_mult({}) == 1.0
    assert adaptive_bucket_cap_mult({"adaptive": {}}) == 1.0
    assert (
        adaptive_bucket_cap_mult(
            {"adaptive": {"bucket_cap_multiplier": {"bullish": 0.0}}},
            regime_condition="bullish",
        )
        == 1.0
    )


def test_adaptive_bucket_cap_mult_condition_wins_over_score() -> None:
    cfg: dict = {"adaptive": {"bucket_cap_multiplier": {"neutral": 1.2, "bullish": 2.0}}}
    assert (
        adaptive_bucket_cap_mult(
            cfg, regime_condition="neutral", regime_score=4
        )
        == pytest.approx(1.2)
    )


def test_effective_max_exposure_uses_regime_table_by_score() -> None:
    cfg: dict = {
        "adaptive": {
            "max_exposure_by_regime": {
                "bullish": 0.98,
                "neutral": 0.95,
                "bearish": 0.85,
            }
        }
    }
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.9, regime_score=4, regime_condition=None
    ) == pytest.approx(0.98)
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.9, regime_score=3, regime_condition=None
    ) == pytest.approx(0.95)
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.9, regime_score=0, regime_condition=None
    ) == pytest.approx(0.85)


def test_effective_max_exposure_regime_3_floor_lifts_neutral_leg() -> None:
    """``adaptive.regime_<n>.max_exposure`` is a floor vs the leg for that exact score."""
    cfg: dict = {
        "adaptive": {
            "max_exposure_by_regime": {
                "neutral": 0.88,
            },
            "regime_3": {"max_exposure": 0.92},
        }
    }
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.9, regime_score=3, regime_condition=None
    ) == pytest.approx(0.92)
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.9, regime_score=2, regime_condition=None
    ) == pytest.approx(0.88)
    cfg2: dict = {
        "adaptive": {
            "max_exposure_by_regime": {
                "neutral": 0.95,
            },
            "regime_3": {"max_exposure": 0.92},
        }
    }
    # Floor below leg — neutral leg applies
    assert adaptive_effective_max_total_exposure(
        cfg2, base_max_total_exposure_frac=0.9, regime_score=3, regime_condition=None
    ) == pytest.approx(0.95)


def test_effective_max_exposure_defensive_uses_bearish_row() -> None:
    cfg: dict = {
        "adaptive": {
            "max_exposure_by_regime": {
                "defensive": 0.7,
            }
        }
    }
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.9, regime_condition="defensive", regime_score=None
    ) == pytest.approx(0.7)
    cfg["adaptive"]["max_exposure_by_regime"]["bearish"] = 0.8
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.9, regime_condition="defensive", regime_score=None
    ) == pytest.approx(0.8)


def test_effective_max_exposure_boost_clamped_at_one() -> None:
    cfg: dict = {
        "adaptive": {
            "max_exposure_by_regime": {"bullish": 0.99},
            "boost_exposure_if_many_signals": True,
            "signal_threshold": 1,
            "boost_amount": 0.1,
            # Preserve legacy test: cap boost at 100%% unless ceiling is raised.
            "max_exposure_frac_ceiling": 1.0,
        }
    }
    assert (
        adaptive_effective_max_total_exposure(
            cfg, base_max_total_exposure_frac=0.5, regime_score=5, entry_wave_strong_signal_count=5
        )
        == 1.0
    )


def test_effective_max_exposure_bullish_score_4_plus_floor() -> None:
    cfg: dict = {
        "adaptive": {
            "max_exposure_by_regime": {
                "bullish": 0.98,
                "neutral": 0.95,
                "bearish": 0.85,
            },
            "bullish_score_4_plus_max_exposure_frac": 1.10,
            "max_exposure_frac_ceiling": 1.25,
        }
    }
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.80, regime_score=4, regime_condition=None
    ) == pytest.approx(1.10)
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.80, regime_score=3, regime_condition=None
    ) == pytest.approx(0.95)
    assert adaptive_effective_max_total_exposure(
        cfg,
        base_max_total_exposure_frac=0.80,
        regime_score=5,
        regime_condition="neutral",
    ) == pytest.approx(0.95)


def test_effective_max_exposure_no_boost_without_count() -> None:
    cfg: dict = {
        "adaptive": {
            "max_exposure_by_regime": {"bullish": 0.9},
            "boost_exposure_if_many_signals": True,
            "signal_threshold": 1,
            "boost_amount": 0.05,
        }
    }
    assert adaptive_effective_max_total_exposure(
        cfg, base_max_total_exposure_frac=0.5, regime_score=4, entry_wave_strong_signal_count=None
    ) == pytest.approx(0.9)
