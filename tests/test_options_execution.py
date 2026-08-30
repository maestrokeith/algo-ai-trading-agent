"""Tests for options order preparation (premium caps + strike fallback)."""

from __future__ import annotations

from datetime import date

from src.options_execution import (
    prepare_option_order_premium_only,
    prepare_option_order_premium_only_with_lower_strike_fallback,
)
from src.options_selector import OptionContractCandidate, SelectedOptionContract


def _base_options_cfg(**overrides: object) -> dict:
    o = {
        "enabled": True,
        "max_total_options_exposure_pct": 40,
        "risk_per_trade_pct": 2,
        "max_option_position_pct": 100,
        "max_open_option_positions": 5,
        "v1_max_contracts_per_trade": 200,
        "premium_over_budget_try_lower_strike": True,
    }
    o.update(overrides)
    return {"portfolio": {"max_options_capital_pct": 40}, "options": o}


def _qqq_call(exp: date, strike: float, bid: float, ask: float) -> OptionContractCandidate:
    """Bid/ask must imply spread %% <= contract_selection.max_bid_ask_spread_pct (5%% in tests)."""
    y = exp.year % 100
    mm = exp.month
    dd = exp.day
    yymmdd = "%02d%02d%02d" % (y, mm, dd)
    strike8 = "%08d" % int(round(strike * 1000))
    sym = "QQQ%sC%s" % (yymmdd, strike8)
    return OptionContractCandidate(
        symbol=sym,
        strike=strike,
        expiration=exp,
        right="call",
        open_interest=100,
        volume=50,
        bid=bid,
        ask=ask,
    )


def test_prepare_strike_fallback_when_atm_premium_too_high() -> None:
    """ATM mid blocks (contracts < 1); next lower strike is cheap enough for the same budget."""
    exp = date(2024, 2, 15)
    chain = [
        _qqq_call(exp, 450.0, 4.95, 5.05),  # mid 5.0 → $500/contract, ~2%% spread
        _qqq_call(exp, 445.0, 0.98, 1.02),  # mid 1.0 → $100/contract, ~4%% spread
    ]
    cfg = _base_options_cfg()
    equity = 10_000.0
    # 2% risk = $200; $500/contract → 0 contracts; $100/contract → 2
    atm = SelectedOptionContract(
        symbol=chain[0].symbol,
        strike=450.0,
        expiration=exp,
        right="call",
        bid=4.95,
        ask=5.05,
        mid=5.0,
        spread_pct=2.0,
        open_interest=100,
        volume=50,
    )
    prep, used, err = prepare_option_order_premium_only_with_lower_strike_fallback(
        cfg,
        equity=equity,
        positions=[],
        chain_candidates=chain,
        selected_atm=atm,
        intent_underlying="QQQ",
        intent_right="call",
        as_of=date(2024, 1, 20),
    )
    assert err is None
    assert prep is not None
    assert used is not None
    assert used.strike == 445.0
    assert prep.contracts >= 1
    assert prep.occ_symbol == used.symbol


def test_prepare_strike_fallback_disabled_returns_atm_error() -> None:
    exp = date(2024, 2, 15)
    chain = [_qqq_call(exp, 450.0, 4.9, 5.1), _qqq_call(exp, 445.0, 0.05, 0.07)]
    cfg = _base_options_cfg(premium_over_budget_try_lower_strike=False)
    atm = SelectedOptionContract(
        symbol=chain[0].symbol,
        strike=450.0,
        expiration=exp,
        right="call",
        bid=4.9,
        ask=5.1,
        mid=5.0,
        spread_pct=4.0,
        open_interest=100,
        volume=50,
    )
    prep, used, err = prepare_option_order_premium_only_with_lower_strike_fallback(
        cfg,
        equity=10_000.0,
        positions=[],
        chain_candidates=chain,
        selected_atm=atm,
        intent_underlying="QQQ",
        intent_right="call",
        as_of=date(2024, 1, 20),
    )
    assert prep is None
    assert used is not None and used.strike == 450.0
    assert err is not None
    assert "premium too expensive" in err


def test_prepare_non_premium_failure_no_strike_walk() -> None:
    """Do not walk strikes when prepare fails for a reason other than premium sizing."""
    exp = date(2024, 2, 15)
    chain = [
        _qqq_call(exp, 450.0, 0.9, 1.1),
        _qqq_call(exp, 445.0, 0.09, 0.11),
    ]
    cfg = _base_options_cfg(max_open_option_positions=0)
    sel = SelectedOptionContract(
        symbol=chain[0].symbol,
        strike=450.0,
        expiration=exp,
        right="call",
        bid=0.9,
        ask=1.1,
        mid=1.0,
        spread_pct=20.0,
        open_interest=100,
        volume=50,
    )
    prep, used, err = prepare_option_order_premium_only_with_lower_strike_fallback(
        cfg,
        equity=50_000.0,
        positions=[],
        chain_candidates=chain,
        selected_atm=sel,
        intent_underlying="QQQ",
        intent_right="call",
        as_of=date(2024, 1, 20),
    )
    assert prep is None
    assert "max open option positions" in str(err)


def test_prepare_option_order_blocked_when_new_entries_disabled() -> None:
    cfg = _base_options_cfg(new_entries_enabled=False)
    sel = SelectedOptionContract(
        symbol="QQQ240215C00450000",
        strike=450.0,
        expiration=date(2024, 2, 15),
        right="call",
        bid=0.95,
        ask=1.05,
        mid=1.0,
        spread_pct=10.0,
        open_interest=100,
        volume=50,
    )
    prep, err = prepare_option_order_premium_only(cfg, equity=25_000.0, positions=[], selected=sel)
    assert prep is None
    assert err is not None
    assert "new entries disabled" in err


def test_prepare_option_order_premium_only_unchanged() -> None:
    cfg = _base_options_cfg()
    sel = SelectedOptionContract(
        symbol="QQQ240215C00450000",
        strike=450.0,
        expiration=date(2024, 2, 15),
        right="call",
        bid=0.9,
        ask=1.1,
        mid=1.0,
        spread_pct=20.0,
        open_interest=100,
        volume=50,
    )
    prep, err = prepare_option_order_premium_only(cfg, equity=25_000.0, positions=[], selected=sel)
    assert err is None
    assert prep is not None
    assert prep.contracts >= 1


def test_prepare_option_order_uses_risk_control_contract_count() -> None:
    cfg = _base_options_cfg(max_premium_per_trade=250, v1_max_contracts_per_trade=10)
    sel = SelectedOptionContract(
        symbol="QQQ240215C00450000",
        strike=450.0,
        expiration=date(2024, 2, 15),
        right="call",
        bid=0.95,
        ask=1.05,
        mid=1.0,
        spread_pct=10.0,
        open_interest=100,
        volume=50,
    )

    prep, err = prepare_option_order_premium_only(cfg, equity=100_000.0, positions=[], selected=sel)

    assert err is None
    assert prep is not None
    assert prep.contracts == 2
