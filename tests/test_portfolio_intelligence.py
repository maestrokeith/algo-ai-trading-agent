"""Tests for portfolio correlation / beta gates."""

from __future__ import annotations

from src.portfolio_intelligence import (
    correlation_resolution_order,
    merged_correlation_groups,
    portfolio_intelligence_blocks_entry,
    resolve_correlation_group,
)


def test_semiconductor_priority_over_tech_for_nvda() -> None:
    cfg = {
        "correlation_groups": {"tech": ["NVDA", "MSFT"], "semiconductors": ["NVDA", "AMD"]},
        "portfolio_intelligence": {
            "correlation_group_priority": ["semiconductors", "tech"],
        },
    }
    merged = merged_correlation_groups(cfg)
    order = correlation_resolution_order(cfg, merged)
    assert resolve_correlation_group("NVDA", merged, order) == "semiconductors"
    assert resolve_correlation_group("MSFT", merged, order) == "tech"


def test_blocks_when_group_symbol_count_at_cap() -> None:
    cfg = {
        "correlation": {"max_per_group": 2},
        "portfolio_intelligence": {
            "enabled": True,
            "max_positions_per_correlation_group": 2,
            "correlation_groups": {"semiconductors": ["NVDA", "AMD", "SMH"]},
            "correlation_group_priority": ["semiconductors"],
        },
    }
    positions = [
        {"symbol": "NVDA", "qty": 10, "market_value": 5000},
        {"symbol": "AMD", "qty": 5, "market_value": 3000},
    ]
    blocked, reason = portfolio_intelligence_blocks_entry(
        "SMH",
        positions=positions,
        account_equity=100_000,
        proposed_notional=2000,
        config=cfg,
    )
    assert blocked is True
    assert reason is not None
    assert "semiconductors" in reason


def test_allows_add_on_same_symbol_when_at_cap() -> None:
    """More shares of a symbol already held does not add a new distinct name."""
    cfg = {
        "correlation": {"max_per_group": 1},
        "portfolio_intelligence": {
            "enabled": True,
            "max_positions_per_correlation_group": 1,
            "correlation_groups": {"semiconductors": ["NVDA", "AMD"]},
            "correlation_group_priority": ["semiconductors"],
        },
    }
    positions = [{"symbol": "NVDA", "qty": 10, "market_value": 8000}]
    blocked, _ = portfolio_intelligence_blocks_entry(
        "NVDA",
        positions=positions,
        account_equity=100_000,
        proposed_notional=1000,
        config=cfg,
    )
    assert blocked is False


def test_beta_cap_blocks_marginal() -> None:
    cfg = {
        "portfolio_intelligence": {
            "enabled": True,
            "max_positions_per_correlation_group": 10,
            "max_beta_units_per_group": 0.12,
            "correlation_groups": {"semiconductors": ["NVDA", "AMD"]},
            "correlation_group_priority": ["semiconductors"],
            "symbol_beta": {"_default": 1.0, "AMD": 2.0},
        },
    }
    positions = [{"symbol": "NVDA", "qty": 1, "market_value": 10_000}]
    # equity 50k → NVDA MV 10k → 0.2 * beta 1.0 = 0.2 beta-units for semis if NVDA resolves — wait NVDA not in list with explicit beta → 1.0 → 10000/50000 * 1 = 0.2
    blocked, reason = portfolio_intelligence_blocks_entry(
        "AMD",
        positions=positions,
        account_equity=50_000,
        proposed_notional=10_000,
        config=cfg,
    )
    # units_now = 0.2; marginal = 10000/50000 * 2.0 = 0.4 → total 0.6 > 0.12
    assert blocked is True
    assert reason is not None
    assert "beta-units" in reason


def test_disabled_no_op() -> None:
    cfg = {"portfolio_intelligence": {"enabled": False}}
    blocked, reason = portfolio_intelligence_blocks_entry(
        "NVDA",
        positions=[],
        account_equity=100_000,
        proposed_notional=5000,
        config=cfg,
    )
    assert blocked is False
    assert reason is None
