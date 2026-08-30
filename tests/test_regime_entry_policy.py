"""Tests for market_regime.entry_policy tiering."""

from __future__ import annotations

from src.regime_entry_policy import (
    compute_regime_entry_policy,
    policy_blocks_sqqq_entry,
    severe_breakdown_ok,
)


def _cfg(**entry_overrides):
    return {
        "market_regime": {
            "entry_policy": {"enabled": True, **entry_overrides},
        }
    }


def test_score_4_full_size_no_severe() -> None:
    p = compute_regime_entry_policy(_cfg(), regime_score=5, regime_scorer_enabled=True)
    assert p.sqqq_notional_fraction == 1.0
    assert p.sqqq_requires_severe_breakdown is False
    assert p.long_notional_fraction == 1.0
    assert p.long_require_ma_stack is False
    assert policy_blocks_sqqq_entry(p, severe_ok=False) == (False, None)


def test_score_3_starter() -> None:
    p = compute_regime_entry_policy(_cfg(), regime_score=3, regime_scorer_enabled=True)
    assert p.sqqq_notional_fraction == 0.5
    assert p.long_notional_fraction == 0.75


def test_score_2_ma_stack() -> None:
    p = compute_regime_entry_policy(_cfg(), regime_score=2, regime_scorer_enabled=True)
    assert p.long_require_ma_stack is True
    assert p.sqqq_notional_fraction == 0.35


def test_score_1_needs_severe_sqqq() -> None:
    p = compute_regime_entry_policy(_cfg(), regime_score=1, regime_scorer_enabled=True)
    assert p.sqqq_requires_severe_breakdown is True
    blocked, r = policy_blocks_sqqq_entry(p, severe_ok=False)
    assert blocked is True and r is not None
    assert policy_blocks_sqqq_entry(p, severe_ok=True) == (False, None)


def test_score_0_blocks_longs_default() -> None:
    p = compute_regime_entry_policy(_cfg(), regime_score=0, regime_scorer_enabled=True)
    assert p.long_entries_blocked is True
    assert p.long_notional_fraction == 0.0


def test_scorer_off_permissove() -> None:
    p = compute_regime_entry_policy(_cfg(), regime_score=None, regime_scorer_enabled=False)
    assert p.sqqq_notional_fraction == 1.0
    assert p.sqqq_requires_severe_breakdown is False


def test_entry_policy_disabled() -> None:
    p = compute_regime_entry_policy(
        {"market_regime": {"entry_policy": {"enabled": False}}},
        regime_score=1,
        regime_scorer_enabled=True,
    )
    assert p.sqqq_requires_severe_breakdown is False


def test_severe_breakdown_math() -> None:
    assert severe_breakdown_ok(
        qqq_price=440.0,
        qqq_ma50=450.0,
        min_pct_below_ma=1.0,
        require_fresh_cross=False,
        qqq_fresh_cross_ma50=None,
    ) is True
    assert severe_breakdown_ok(
        qqq_price=448.0,
        qqq_ma50=450.0,
        min_pct_below_ma=1.0,
        require_fresh_cross=False,
        qqq_fresh_cross_ma50=None,
    ) is False
