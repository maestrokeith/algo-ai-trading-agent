"""Tests for options premium / OCC symbol helpers."""

from __future__ import annotations

import pytest

from src.options_premium_risk import (
    evaluate_options_premium_before_order,
    evaluate_options_order_risk_controls,
    holding_equity_long_for_underlying,
    is_option_position,
    is_option_symbol,
    max_premium_budget_usd,
)


def test_is_option_symbol_qqq_put() -> None:
    assert is_option_symbol("QQQ260417P00567000") is True


def test_is_option_symbol_equity_false() -> None:
    assert is_option_symbol("QQQ") is False
    assert is_option_symbol("") is False


def test_holding_equity_long_for_underlying_positions() -> None:
    pos = [{"symbol": "NVDA", "qty": 5}]
    assert holding_equity_long_for_underlying("NVDA", pos, None) is True
    assert holding_equity_long_for_underlying("nvda", pos, None) is True
    assert holding_equity_long_for_underlying("SMH", pos, None) is False


def test_holding_equity_long_for_underlying_ignores_option_symbol_rows() -> None:
    occ = "NVDA260417C00100000"
    pos = [{"symbol": occ, "qty": 1}]
    assert holding_equity_long_for_underlying("NVDA", pos, None) is False


def test_holding_equity_long_for_underlying_tracked() -> None:
    assert holding_equity_long_for_underlying("NVDA", [], {"NVDA": {"qty": 2}}) is True
    assert holding_equity_long_for_underlying("NVDA", [], {"NVDA": {"qty": 0}}) is False


def test_is_option_position_broker_row() -> None:
    assert is_option_position({"symbol": "QQQ260417P00567000", "qty": 1}) is True
    assert is_option_position({"symbol": "QQQ", "qty": 10}) is False
    assert is_option_position({}) is False
    assert is_option_position(None) is False


def test_max_premium_budget_usd_matches_evaluate_ceiling() -> None:
    equity = 100_000.0
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "max_premium_pct_of_equity": 0.02,
            "max_option_position_pct": 100,
            "max_premium_per_trade": 150,
        },
    }
    b, err = max_premium_budget_usd(cfg, equity=equity, positions=[])
    assert err is None
    assert b == pytest.approx(150.0)


def test_evaluate_options_premium_risk_per_trade_two_percent() -> None:
    """contracts = floor(0.02 * equity / (mid * 100)) when room and ceiling allow."""
    equity = 100_000.0
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "risk_per_trade_pct": 2,
            "max_option_position_pct": 100,
        },
    }
    # 2% = $2000; mid $2 → $200/contract → 10 contracts
    r = evaluate_options_premium_before_order(
        cfg, equity=equity, positions=[], option_mid_price=2.0
    )
    assert r.ok is True
    assert r.contracts == 10


def test_evaluate_options_premium_respects_v1_max_contracts() -> None:
    equity = 100_000.0
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "risk_per_trade_pct": 10,
            "max_option_position_pct": 100,
            "v1_max_contracts_per_trade": 3,
        },
    }
    # 10% = $10k; mid $1 → 100 contracts possible; cap 3
    r = evaluate_options_premium_before_order(
        cfg, equity=equity, positions=[], option_mid_price=1.0
    )
    assert r.ok is True
    assert r.contracts == 3


def test_evaluate_options_premium_max_premium_per_trade_dollar_cap() -> None:
    equity = 100_000.0
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "max_premium_pct_of_equity": 0.10,
            "max_premium_per_trade": 300,
            "max_option_position_pct": 100,
        },
    }
    # 10% equity = $10k budget but $300 cap → 1 contract at mid $2 ($200) or 6 at $0.5 ($50 each)
    r = evaluate_options_premium_before_order(
        cfg, equity=equity, positions=[], option_mid_price=0.5
    )
    assert r.ok is True
    assert r.contracts == 6


def test_evaluate_options_premium_max_option_position_ceiling() -> None:
    equity = 50_000.0
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "risk_per_trade_pct": 10,
            "max_option_position_pct": 2,
        },
    }
    # risk 10% = $5k but ceiling 2% = $1k; mid $1 → 10 contracts
    r = evaluate_options_premium_before_order(
        cfg, equity=equity, positions=[], option_mid_price=1.0
    )
    assert r.ok is True
    assert r.contracts == 10


def test_max_option_position_pct_fraction_alias() -> None:
    equity = 50_000.0
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "risk_per_trade_pct": 10,
            "max_option_position_pct": 0.02,
        },
    }
    r = evaluate_options_premium_before_order(
        cfg, equity=equity, positions=[], option_mid_price=1.0
    )
    assert r.ok is True
    assert r.contracts == 10


def test_max_contracts_per_trade_alias() -> None:
    equity = 100_000.0
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "risk_per_trade_pct": 10,
            "max_contracts_per_trade": 3,
        },
    }
    r = evaluate_options_premium_before_order(
        cfg, equity=equity, positions=[], option_mid_price=0.01
    )
    assert r.ok is True
    assert r.contracts <= 3


def test_options_order_risk_controls_report_max_loss_and_budget() -> None:
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "max_premium_pct_of_equity": 0.10,
            "max_premium_per_trade": 450,
            "max_option_position_pct": 100,
            "max_contracts_per_trade": 10,
            "max_daily_loss_pct": 1.0,
        },
    }

    result = evaluate_options_order_risk_controls(
        cfg,
        equity=100_000.0,
        positions=[],
        option_mid_price=2.0,
    )

    assert result.ok is True
    assert result.contracts == 2
    assert result.premium_budget_usd == pytest.approx(450.0)
    assert result.max_loss_usd == pytest.approx(400.0)
    assert result.daily_loss_limit_usd == pytest.approx(1_000.0)


def test_options_order_risk_controls_block_daily_option_loss() -> None:
    cfg = {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "max_premium_pct_of_equity": 0.10,
            "max_premium_per_trade": 1_000,
            "max_daily_option_loss_pct": 0.5,
        },
    }

    result = evaluate_options_order_risk_controls(
        cfg,
        equity=50_000.0,
        positions=[],
        option_mid_price=1.0,
        daily_options_realized_pl=-260.0,
    )

    assert result.ok is False
    assert result.contracts == 0
    assert result.daily_loss_limit_usd == pytest.approx(250.0)
    assert "daily options loss" in str(result.reason)
