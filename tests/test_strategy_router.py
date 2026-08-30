"""Strategy router: options vs shares."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.entry_router import EntryRouteSignal
from src.options_selector import OptionContractCandidate
from src.strategy_router import find_option_under_budget, route_options_or_shares


def _minimal_options_config() -> dict:
    return {
        "options": {
            "enabled": True,
            "mode": "long_premium_only",
            "allowed_underlyings": ["NVDA"],
            "entry_mapping": {"bullish_signal": "call"},
            "max_premium_pct_of_equity": 0.02,
            "max_total_options_exposure_pct": 40,
            "max_option_position_pct": 100,
            "max_premium_per_trade": 5000,
            "contract_selection": {
                "expiry_min_days": 7,
                "expiry_max_days": 60,
                "moneyness": "ATM",
                "max_bid_ask_spread_pct": 5.0,
                "min_open_interest": 0,
                "min_volume": 0,
            },
        },
        "portfolio": {"max_options_capital_pct": 40},
    }


def _chain_nvda(*, bid: float = 4.0, ask: float = 4.2, strike: float = 100.0) -> list[OptionContractCandidate]:
    exp = date.today() + timedelta(days=14)
    occ = "NVDA%sC00100000" % exp.strftime("%y%m%d")
    return [
        OptionContractCandidate(
            symbol=occ,
            strike=strike,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=bid,
            ask=ask,
        )
    ]


def test_route_options_or_shares_returns_options_when_contract() -> None:
    cfg = _minimal_options_config()
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )
    out = route_options_or_shares(
        42,
        options_enabled=True,
        config=cfg,
        signal=sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=date.today(),
    )
    assert out.leg == "options"
    assert out.option_contract is not None
    assert out.option_contract.right == "call"
    assert out.share_size == 0


def test_route_options_or_shares_returns_shares_when_disabled() -> None:
    cfg = _minimal_options_config()
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )
    out = route_options_or_shares(
        7,
        options_enabled=False,
        config=cfg,
        signal=sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=date.today(),
    )
    assert out.leg == "shares"
    assert out.option_contract is None
    assert out.share_size == 7


def test_find_option_under_budget_none_when_holding_underlying_equity() -> None:
    cfg = _minimal_options_config()
    sig = MagicMock()
    sig.underlying = "NVDA"
    sig.direction = "bullish"
    sig.source = "trend_long"
    sig.stock_symbol = "NVDA"
    positions = [{"symbol": "NVDA", "qty": 10}]
    c, err = find_option_under_budget(
        cfg,
        sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=100_000.0,
        positions=positions,
        as_of=date.today(),
    )
    assert c is None
    assert err is not None and "holding equity" in err.lower()


def test_route_options_or_shares_falls_back_to_shares_when_holding_equity() -> None:
    cfg = _minimal_options_config()
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )
    positions = [{"symbol": "NVDA", "qty": 3}]
    out = route_options_or_shares(
        11,
        options_enabled=True,
        config=cfg,
        signal=sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=100_000.0,
        positions=positions,
        as_of=date.today(),
    )
    assert out.leg == "shares"
    assert out.share_size == 11
    assert out.option_contract is None
    assert out.options_select_error is not None


def test_route_options_or_shares_falls_back_to_stock_when_no_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _minimal_options_config()
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )

    def _stub_find(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None, "no option contract passed filters"

    monkeypatch.setattr("src.strategy_router.find_option_under_budget", _stub_find)

    out = route_options_or_shares(
        13,
        options_enabled=True,
        config=cfg,
        signal=sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=date.today(),
    )
    assert out.leg == "shares"
    assert out.share_size == 13


def test_route_options_or_shares_paper_only_stays_in_options_leg_when_no_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _minimal_options_config()
    cfg["options"]["mode"] = "paper_only"
    sig = EntryRouteSignal(
        underlying="NVDA",
        direction="bullish",
        source="trend_long",
        stock_symbol="NVDA",
    )

    def _stub_find(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None, "no option contract passed filters"

    monkeypatch.setattr("src.strategy_router.find_option_under_budget", _stub_find)

    out = route_options_or_shares(
        13,
        options_enabled=True,
        config=cfg,
        signal=sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=date.today(),
    )
    assert out.leg == "options"
    assert out.option_contract is None
    assert out.share_size == 0
    assert out.options_select_error is not None
    assert out.option_contract is None
    assert out.options_select_error == "no option contract passed filters"


def test_find_option_under_budget_scales_equity_with_capital_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _minimal_options_config()
    cfg["portfolio"]["capital_split"] = {
        "enabled": True,
        "stocks": 0.7,
        "options": 0.3,
    }
    captured: dict[str, float | None] = {}

    def _stub_select(config, **kw):  # type: ignore[no-untyped-def]
        captured["equity"] = kw.get("equity")
        captured["premium_budget_cap_usd"] = kw.get("premium_budget_cap_usd")
        return None, "stub"

    monkeypatch.setattr(
        "src.strategy_router.select_first_ranked_candidate_within_budget",
        _stub_select,
    )
    sig = MagicMock()
    sig.underlying = "NVDA"
    sig.direction = "bullish"
    sig.source = "trend_long"
    sig.stock_symbol = "NVDA"
    find_option_under_budget(
        cfg,
        sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=date.today(),
        premium_budget_cap_usd=5000.0,
    )
    assert captured.get("equity") == pytest.approx(30_000.0)
    assert captured.get("premium_budget_cap_usd") == pytest.approx(1500.0)


def test_find_option_under_budget_none_without_equity() -> None:
    cfg = _minimal_options_config()
    sig = MagicMock()
    sig.underlying = "NVDA"
    sig.direction = "bullish"
    sig.source = "trend_long"
    sig.stock_symbol = "NVDA"
    c, err = find_option_under_budget(
        cfg,
        sig,
        chain_candidates=_chain_nvda(),
        underlying_spot=100.0,
        equity=None,
        positions=[],
        as_of=date.today(),
    )
    assert c is None
    assert err is not None
