"""Tests for phase-1 portfolio_brain concentration gates."""

from __future__ import annotations

import pytest

from src.portfolio_brain import (
    bucket_exposure_frac,
    portfolio_brain,
    portfolio_brain_enabled,
    symbol_exposure_frac,
)
from src.signal_ranking import sector_etf_symbol_frozenset


def test_portfolio_brain_enabled_default_off() -> None:
    assert portfolio_brain_enabled({}) is False
    assert portfolio_brain_enabled({"portfolio": {"portfolio_brain": {"enabled": True}}}) is True


def test_bucket_exposure_frac_named_bucket() -> None:
    cfg = {"risk_buckets": {"mega_cap_beta": ["SPY", "MSFT"]}}
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "SPY", "qty": 1, "market_value": 25_000.0}]
    f = bucket_exposure_frac(
        "mega_cap_beta",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert f == pytest.approx(0.25)


def test_portfolio_brain_ok_empty_book() -> None:
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_new_positions_per_cycle": 2},
        "portfolio": {"max_allocation_per_symbol": "10%"},
    }
    se = sector_etf_symbol_frozenset({})
    d = portfolio_brain(
        "MSFT",
        positions=[],
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert d["allow_new_positions"] is True
    assert d["max_new_trades"] == 2
    assert d["symbol_allowed"] is True
    assert d["reason"] == "ok"


def test_portfolio_brain_high_cash_loosens_bucket_cap() -> None:
    """Same book as bucket block, but high_cash_deploy widens bucket ceiling."""
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.50},
        "risk_buckets": {"mega_cap_beta": ["SPY", "MSFT", "NVDA"]},
        "portfolio": {
            "portfolio_brain": {
                "enabled": True,
                "when_high_cash": {"bucket_cap_mult": 1.1},
            }
        },
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "SPY", "qty": 100, "market_value": 31_000.0}]
    blocked = portfolio_brain(
        "MSFT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
        high_cash_deploy=False,
    )
    assert blocked["symbol_allowed"] is False
    loose = portfolio_brain(
        "MSFT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
        high_cash_deploy=True,
    )
    assert loose["symbol_allowed"] is True


def test_portfolio_brain_mega_cap_beta_cap_loosens_bucket() -> None:
    """31% mega bucket book: global 30% blocks; ``mega_cap_beta_cap`` 45% allows."""
    cfg = {
        "risk": {
            "max_bucket_allocation_pct": 0.30,
            "mega_cap_beta_cap": 45,
            "max_symbol_allocation_pct": 0.50,
        },
        "risk_buckets": {"mega_cap_beta": ["SPY", "MSFT", "NVDA"]},
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "SPY", "qty": 100, "market_value": 31_000.0}]
    d = portfolio_brain(
        "MSFT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert d["symbol_allowed"] is True
    assert d["reason"] == "ok"


def test_portfolio_brain_regime_effective_bucket_allows() -> None:
    """``adaptive.bucket_cap_multiplier`` (neutral) widens bucket ceiling as in risk_limits."""
    cfg: dict = {
        "risk": {
            "max_bucket_allocation_pct": 0.30,
            "max_symbol_allocation_pct": 0.50,
        },
        "risk_buckets": {"mega_cap_beta": ["SPY", "MSFT", "NVDA"]},
        "adaptive": {
            "bucket_cap_multiplier": {
                "neutral": 1.2,
                "bullish": 1.0,
                "bearish": 1.0,
            }
        },
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "SPY", "qty": 100, "market_value": 31_000.0}]
    blocked = portfolio_brain(
        "MSFT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert blocked["symbol_allowed"] is False
    loose = portfolio_brain(
        "MSFT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
        regime_score=2,
    )
    assert loose["symbol_allowed"] is True


def test_portfolio_brain_cross_bucket_rebalance_allows_when_other_sleeve_has_slack() -> None:
    """``tier_3`` at base cap: portfolio_brain may still pass when another sleeve (mega) is under its cap."""
    cfg: dict = {
        "risk": {
            "max_bucket_allocation_pct": 0.30,
            "max_symbol_allocation_pct": 0.5,
            "mega_cap_beta_cap": 45,
        },
        "risk_buckets": {
            "mega_cap_beta": [
                "MSFT",
                "AAPL",
                "SPY",
                "NVDA",
                "AMZN",
                "GOOGL",
                "META",
            ],
        },
        "portfolio": {
            "allocator": {"allow_cross_bucket_rebalance": True},
        },
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "CAT", "qty": 100, "market_value": 30_000.0}]
    d = portfolio_brain(
        "CAT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert d["symbol_allowed"] is True
    assert d["reason"] == "ok"


def test_portfolio_brain_top_signal_bypasses_bucket() -> None:
    cfg: dict = {
        "risk": {
            "max_bucket_allocation_pct": 0.30,
            "max_symbol_allocation_pct": 0.50,
        },
        "risk_buckets": {"mega_cap_beta": ["SPY", "MSFT", "NVDA"]},
        "execution": {
            "allow_bucket_override_for_top_signals": True,
            "top_signal_percentile": 0.2,
        },
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "SPY", "qty": 100, "market_value": 31_000.0}]
    cohort = [0.5, 0.6, 0.7, 0.8, 0.9]
    d = portfolio_brain(
        "MSFT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
        entry_strength=0.9,
        strength_cohort=cohort,
    )
    assert d["symbol_allowed"] is True


def test_portfolio_brain_blocks_bucket_over_cap() -> None:
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.30, "max_symbol_allocation_pct": 0.50},
        "risk_buckets": {"mega_cap_beta": ["SPY", "MSFT", "NVDA"]},
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "SPY", "qty": 100, "market_value": 31_000.0}]
    d = portfolio_brain(
        "MSFT",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert d["symbol_allowed"] is False
    assert "bucket limit hit" in d["reason"]
    assert "mega_cap_beta" in d["reason"]


def test_portfolio_brain_blocks_symbol_over_cap() -> None:
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.99, "max_symbol_allocation_pct": 0.10},
        "portfolio": {"max_allocation_per_symbol": "10%"},
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "AAPL", "qty": 10, "market_value": 11_000.0}]
    d = portfolio_brain(
        "AAPL",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert d["symbol_allowed"] is False
    assert "symbol cap hit" in d["reason"]


def test_portfolio_brain_blocks_sector_overexposed() -> None:
    cfg = {
        "risk": {"max_bucket_allocation_pct": 0.99, "max_symbol_allocation_pct": 0.50},
        "position_sizing": {"max_exposure_per_sector_pct": 35.0},
    }
    se = sector_etf_symbol_frozenset({})
    d = portfolio_brain(
        "NVDA",
        positions=[],
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
        sector_exposure_pct={"Technology": 36.0},
        symbol_sector={"NVDA": "Technology"},
    )
    assert d["symbol_allowed"] is False
    assert "Technology overexposed" in d["reason"]


def test_symbol_exposure_frac() -> None:
    positions = [{"symbol": "QQQ", "qty": 1, "market_value": 5000.0}]
    assert symbol_exposure_frac("QQQ", positions=positions, equity=100_000.0) == pytest.approx(0.05)


def test_portfolio_brain_invalid_equity() -> None:
    d = portfolio_brain("SPY", positions=[], equity=0.0, config={}, sector_etfs=frozenset())
    assert d["allow_new_positions"] is False
    assert d["symbol_allowed"] is False
    assert "equity" in d["reason"].lower()


def test_portfolio_brain_skip_symbol_allocation_cap_gate() -> None:
    cfg = {
        "portfolio": {"max_single_position_pct": 10.0},
        "risk": {"max_symbol_allocation_pct": 0.10},
    }
    se = sector_etf_symbol_frozenset({})
    positions = [{"symbol": "QQQ", "qty": 10, "market_value": 11_000.0}]
    blocked = portfolio_brain(
        "QQQ",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
    )
    assert blocked["symbol_allowed"] is False
    ok = portfolio_brain(
        "QQQ",
        positions=positions,
        equity=100_000.0,
        config=cfg,
        sector_etfs=se,
        skip_symbol_allocation_cap_gate=True,
    )
    assert ok["symbol_allowed"] is True
    assert ok["reason"] == "ok"
