"""Tests for options chain selection and liquidity validation."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.options_selector import (
    OptionContractCandidate,
    SelectedOptionContract,
    build_candidates,
    candidate_to_selected_contract,
    lower_strike_candidates_same_series,
    rank_candidates_atm_then_cheaper,
    select_first_ranked_candidate_within_budget,
    select_option_contract,
    validate_option_liquidity,
)


def _sample_config(*, min_oi: int = 0, min_vol: int = 0) -> dict:
    return {
        "options": {
            "contract_selection": {
                "expiry_min_days": 14,
                "expiry_max_days": 35,
                "moneyness": "ATM",
                "max_bid_ask_spread_pct": 5.0,
                "min_open_interest": min_oi,
                "min_volume": min_vol,
            }
        }
    }


def _selected(*, oi: int = 0, vol: int = 0, spread_pct: float = 1.0) -> SelectedOptionContract:
    return SelectedOptionContract(
        symbol="TEST240119C00100000",
        strike=100.0,
        expiration=date(2024, 1, 19),
        right="call",
        bid=2.0,
        ask=2.2,
        mid=2.1,
        spread_pct=spread_pct,
        open_interest=oi,
        volume=vol,
    )


class TestValidateOptionLiquidity:
    def test_oi_missing_zero_passes_even_if_min_configured(self) -> None:
        """Alpaca-style OI=0 when snapshot has no OI must not trip a high min_open_interest."""
        cfg = _sample_config(min_oi=500, min_vol=0)
        ok, reason = validate_option_liquidity(_selected(oi=0, vol=0), cfg)
        assert ok is True
        assert reason == "ok"

    def test_oi_known_below_min_fails(self) -> None:
        cfg = _sample_config(min_oi=500, min_vol=0)
        ok, reason = validate_option_liquidity(_selected(oi=100, vol=0), cfg)
        assert ok is False
        assert "open_interest" in reason

    def test_oi_known_at_min_passes(self) -> None:
        cfg = _sample_config(min_oi=500, min_vol=0)
        ok, _ = validate_option_liquidity(_selected(oi=500, vol=0), cfg)
        assert ok is True

    def test_min_open_interest_zero_always_passes_oi(self) -> None:
        cfg = _sample_config(min_oi=0, min_vol=0)
        ok, _ = validate_option_liquidity(_selected(oi=0, vol=0), cfg)
        assert ok is True

    def test_volume_gate(self) -> None:
        cfg = _sample_config(min_oi=0, min_vol=100)
        ok, reason = validate_option_liquidity(_selected(oi=0, vol=50), cfg)
        assert ok is False
        assert "volume" in reason


def test_candidate_spread_uses_absolute_mid_for_crossed_quote() -> None:
    cfg = _sample_config()
    cfg["options"]["contract_selection"]["max_bid_ask_spread_pct"] = 10.0
    selected, err = candidate_to_selected_contract(
        cfg,
        OptionContractCandidate(
            symbol="QQQ240215C00450000",
            strike=450.0,
            expiration=date(2024, 2, 15),
            right="call",
            open_interest=100,
            volume=50,
            bid=2.2,
            ask=2.0,
        ),
        "call",
    )
    assert err is None
    assert selected is not None
    assert selected.mid == pytest.approx(2.1)
    assert selected.spread_pct == pytest.approx(abs(2.0 - 2.2) / 2.1 * 100.0)


def test_candidate_rejects_unstable_quote_even_when_config_cap_is_loose(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _sample_config()
    cfg["options"]["contract_selection"]["max_bid_ask_spread_pct"] = 50.0

    with caplog.at_level("WARNING"):
        selected, err = candidate_to_selected_contract(
            cfg,
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=date(2024, 2, 15),
                right="call",
                open_interest=100,
                volume=50,
                bid=1.0,
                ask=1.2,
            ),
            "call",
        )

    assert selected is None
    assert err is not None and "unstable quote" in err
    assert "Unstable quote QQQ240215C00450000" in caplog.text


def _rank_scan_config() -> dict:
    return {
        "portfolio": {"max_options_capital_pct": 40},
        "options": {
            "max_total_options_exposure_pct": 40,
            "max_premium_pct_of_equity": 0.02,
            "max_option_position_pct": 100,
            "contract_selection": {
                "expiry_min_days": 14,
                "expiry_max_days": 35,
                "moneyness": "ATM",
                "max_bid_ask_spread_pct": 5.0,
                "min_open_interest": 0,
                "min_volume": 0,
            },
        },
    }


class TestRankedBudgetScan:
    def test_select_first_skips_atm_when_over_budget_picks_cheaper(self) -> None:
        """ATM row over budget; scan picks next-ranked cheaper contract."""
        exp = date(2024, 2, 15)
        cfg = _rank_scan_config()
        chain = [
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=exp,
                right="call",
                open_interest=100,
                volume=50,
                bid=4.95,
                ask=5.05,
            ),
            OptionContractCandidate(
                symbol="QQQ240215C00447000",
                strike=447.0,
                expiration=exp,
                right="call",
                open_interest=100,
                volume=50,
                bid=0.98,
                ask=1.02,
            ),
        ]
        sel, err = select_first_ranked_candidate_within_budget(
            cfg,
            intent_underlying="QQQ",
            intent_right="call",
            chain=chain,
            underlying_spot=450.0,
            equity=10_000.0,
            positions=[],
            as_of=date(2024, 1, 20),
        )
        assert err is None
        assert sel is not None
        assert float(sel.strike) == 447.0

    def test_select_first_respects_premium_budget_cap_usd(self) -> None:
        """``premium_budget_cap_usd`` tightens effective budget (portfolio-full second pass)."""
        exp = date(2024, 2, 15)
        cfg = _rank_scan_config()
        chain = [
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=exp,
                right="call",
                open_interest=100,
                volume=50,
                bid=0.498,
                ask=0.502,
            ),
        ]
        sel_ok, err_ok = select_first_ranked_candidate_within_budget(
            cfg,
            intent_underlying="QQQ",
            intent_right="call",
            chain=list(chain),
            underlying_spot=450.0,
            equity=10_000.0,
            positions=[],
            as_of=date(2024, 1, 20),
            premium_budget_cap_usd=60.0,
        )
        assert err_ok is None and sel_ok is not None
        sel_none, err_none = select_first_ranked_candidate_within_budget(
            cfg,
            intent_underlying="QQQ",
            intent_right="call",
            chain=list(chain),
            underlying_spot=450.0,
            equity=10_000.0,
            positions=[],
            as_of=date(2024, 1, 20),
            premium_budget_cap_usd=40.0,
        )
        assert sel_none is None
        assert err_none is not None

    def test_build_candidates_strike_filter(self) -> None:
        exp = date(2024, 2, 15)
        cfg = _sample_config()
        chain = [
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=exp,
                right="call",
                open_interest=0,
                volume=50,
                bid=1.0,
                ask=1.1,
            ),
            OptionContractCandidate(
                symbol="QQQ240215C00447000",
                strike=447.0,
                expiration=exp,
                right="call",
                open_interest=0,
                volume=50,
                bid=1.0,
                ask=1.1,
            ),
        ]
        built = build_candidates(
            "QQQ",
            chain,
            None,
            [450.0],
            want_right="call",
            config=cfg,
            as_of=date(2024, 1, 20),
        )
        assert len(built) == 1
        assert float(built[0].strike) == 450.0

    def test_rank_candidates_orders_by_atm_distance_then_mid_cost(self) -> None:
        exp = date(2024, 2, 15)
        a = OptionContractCandidate(
            symbol="QQQ240215C00450000",
            strike=450.0,
            expiration=exp,
            right="call",
            open_interest=0,
            volume=50,
            bid=2.0,
            ask=2.2,
        )
        b = OptionContractCandidate(
            symbol="QQQ240215C00447000",
            strike=447.0,
            expiration=exp,
            right="call",
            open_interest=0,
            volume=50,
            bid=1.0,
            ask=1.1,
        )
        r = rank_candidates_atm_then_cheaper([b, a], 450.0)
        assert r[0].strike == 450.0
        assert r[1].strike == 447.0


class TestSelectOptionContract:
    def test_atm_select_passes_liquidity_with_zero_oi(self) -> None:
        cfg = _sample_config(min_oi=0, min_vol=0)
        cands = [
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=date(2024, 2, 15),
                right="call",
                open_interest=0,
                volume=50,
                bid=5.0,
                ask=5.1,
            )
        ]
        sel, err = select_option_contract(
            cfg,
            "QQQ",
            "call",
            candidates=cands,
            underlying_spot=450.0,
            as_of=date(2024, 1, 20),
        )
        assert err is None
        assert sel is not None
        assert sel.symbol == "QQQ240215C00450000"

    def test_selects_best_scored_contract_and_logs_scores(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _sample_config(min_oi=0, min_vol=0)
        cands = [
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=date(2024, 2, 15),
                right="call",
                open_interest=550,
                volume=120,
                bid=2.2,
                ask=2.3,
                delta=0.42,
                iv=0.95,
            ),
            OptionContractCandidate(
                symbol="QQQ240215C00450001",
                strike=450.0,
                expiration=date(2024, 2, 15),
                right="call",
                open_interest=2200,
                volume=700,
                bid=1.0,
                ask=1.05,
                delta=0.50,
                iv=0.30,
            ),
        ]
        caplog.set_level("INFO")
        sel, err = select_option_contract(
            cfg,
            "QQQ",
            "call",
            candidates=cands,
            underlying_spot=450.0,
            as_of=date(2024, 1, 20),
            signal=SimpleNamespace(conviction_score=8.0, news_score=6.0, event_score=3.0, relative_volume=2.5),
        )
        assert err is None
        assert sel is not None
        assert sel.symbol == "QQQ240215C00450001"
        assert "OPTIONS_CHAIN_SCANNED symbol=QQQ right=call" in caplog.text
        assert "OPTIONS_CONTRACT_SELECTED symbol=QQQ right=call contract=QQQ240215C00450001" in caplog.text
        assert "OPTIONS_CONTRACT_SCORE symbol=QQQ right=call" in caplog.text

    def test_selected_contract_reason_codes_include_catalyst_boost(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = _sample_config(min_oi=0, min_vol=0)
        cands = [
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=date(2024, 2, 15),
                right="call",
                open_interest=1500,
                volume=500,
                bid=1.0,
                ask=1.03,
                delta=0.50,
            ),
        ]
        caplog.set_level("INFO")

        sel, err = select_option_contract(
            cfg,
            "QQQ",
            "call",
            candidates=cands,
            underlying_spot=450.0,
            as_of=date(2024, 1, 20),
            signal=SimpleNamespace(news_score=8.0, event_score=7.5, catalyst_type="earnings"),
        )

        assert err is None
        assert sel is not None
        assert "catalyst_boost" in caplog.text
        assert "catalyst=" in caplog.text


class TestLowerStrikeCandidatesSameSeries:
    def test_orders_closest_lower_strike_first(self) -> None:
        cfg = _sample_config()
        exp = date(2024, 2, 15)
        ref = SelectedOptionContract(
            symbol="QQQ240215C00450000",
            strike=450.0,
            expiration=exp,
            right="call",
            bid=1.0,
            ask=1.1,
            mid=1.05,
            spread_pct=9.0,
            open_interest=10,
            volume=50,
        )
        chain = [
            OptionContractCandidate(
                symbol="QQQ240215C00450000",
                strike=450.0,
                expiration=exp,
                right="call",
                open_interest=10,
                volume=50,
                bid=1.0,
                ask=1.1,
            ),
            OptionContractCandidate(
                symbol="QQQ240215C00440000",
                strike=440.0,
                expiration=exp,
                right="call",
                open_interest=10,
                volume=50,
                bid=0.5,
                ask=0.6,
            ),
            OptionContractCandidate(
                symbol="QQQ240215C00447000",
                strike=447.0,
                expiration=exp,
                right="call",
                open_interest=10,
                volume=50,
                bid=0.7,
                ask=0.8,
            ),
        ]
        alts = lower_strike_candidates_same_series(
            cfg,
            "QQQ",
            "call",
            chain,
            ref,
            as_of=date(2024, 1, 20),
        )
        assert [float(c.strike) for c in alts] == [447.0, 440.0]

    def test_respects_dte_window(self) -> None:
        cfg = _sample_config()
        exp_far = date(2024, 6, 15)
        exp_ok = date(2024, 2, 15)
        ref = SelectedOptionContract(
            symbol="QQQ240215C00450000",
            strike=450.0,
            expiration=exp_ok,
            right="call",
            bid=1.0,
            ask=1.1,
            mid=1.05,
            spread_pct=9.0,
            open_interest=10,
            volume=50,
        )
        chain = [
            OptionContractCandidate(
                symbol="QQQ240615C00440000",
                strike=440.0,
                expiration=exp_far,
                right="call",
                open_interest=10,
                volume=50,
                bid=0.5,
                ask=0.6,
            ),
        ]
        alts = lower_strike_candidates_same_series(
            cfg,
            "QQQ",
            "call",
            chain,
            ref,
            as_of=date(2024, 1, 20),
        )
        assert alts == []


def test_select_first_ranked_skips_low_delta_then_picks_qualifying() -> None:
    cfg = _sample_config()
    cfg["options"]["min_delta"] = 0.4
    as_of = date(2024, 1, 20)
    exp = date(2024, 2, 15)
    chain = [
        OptionContractCandidate(
            symbol="QQQ240215C00100000",
            strike=100.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.25,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00105000",
            strike=105.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.48,
        ),
    ]
    sel, err = select_first_ranked_candidate_within_budget(
        cfg,
        intent_underlying="QQQ",
        intent_right="call",
        chain=chain,
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=as_of,
    )
    assert err is None
    assert sel is not None
    assert sel.strike == pytest.approx(105.0)


def test_select_first_ranked_min_delta_no_greeks_returns_reason() -> None:
    cfg = _sample_config()
    cfg["options"]["min_delta"] = 0.4
    as_of = date(2024, 1, 20)
    exp = date(2024, 2, 15)
    chain = [
        OptionContractCandidate(
            symbol="QQQ240215C00100000",
            strike=100.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.1,
        ),
    ]
    sel, err = select_first_ranked_candidate_within_budget(
        cfg,
        intent_underlying="QQQ",
        intent_right="call",
        chain=chain,
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=as_of,
    )
    assert sel is None
    assert err is not None and "greeks" in (err or "").lower()


def test_select_first_ranked_logs_rejected_contracts(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _sample_config()
    cfg["options"]["min_delta"] = 0.4
    as_of = date(2024, 1, 20)
    exp = date(2024, 2, 15)
    chain = [
        OptionContractCandidate(
            symbol="QQQ240215C00100000",
            strike=100.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.25,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00105000",
            strike=105.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.48,
        ),
    ]
    caplog.set_level("INFO")
    sel, err = select_first_ranked_candidate_within_budget(
        cfg,
        intent_underlying="QQQ",
        intent_right="call",
        chain=chain,
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=as_of,
    )
    assert err is None
    assert sel is not None
    assert "OPTION_SCAN_START symbol=QQQ right=call chain_rows=2 path=ranked_budget" in caplog.text
    assert "OPTIONS_CHAIN_SCANNED symbol=QQQ right=call" in caplog.text
    assert "OPTIONS_CONTRACT_REJECTED symbol=QQQ right=call" in caplog.text
    assert "OPTION_SCAN_SUMMARY symbol=QQQ chain_n=2 selected=1" in caplog.text
    assert "OPTIONS_FUNNEL underlying=QQQ underlyings_seen=1 chains_loaded=1 contracts_examined=2" in caplog.text
    assert "contracts_after_dte=2" in caplog.text
    assert "contracts_after_delta=1" in caplog.text
    assert "delta_rejects=1" in caplog.text
    assert "stale_quote_rejects=0" in caplog.text
    assert "contracts_rejected_delta=1" in caplog.text
    assert "delta_failed=1" in caplog.text
    assert "OPTIONS_CHAIN_SUMMARY underlying=QQQ direction=call chain_size=2 spot_price=100 dte_range_used=14-35" in caplog.text
    assert "budget_used=" in caplog.text
    assert "selected_count=1 surviving_contracts=1 top_rejection_reason=delta_failed" in caplog.text
    assert "OPTION_CANDIDATE_REJECT underlying=QQQ contract=QQQ240215C00100000" in caplog.text
    assert "OPTION_NEAR_MISS underlying=QQQ contract=QQQ240215C00100000" in caplog.text
    assert "option_symbol=QQQ240215C00100000" in caplog.text
    assert "call_put=call" in caplog.text
    assert "mid=1.01" in caplog.text
    assert "estimated_cost=101.00" in caplog.text
    assert "OPTION_REJECT_DETAIL symbol=QQQ contract=QQQ240215C00100000" in caplog.text
    assert "OPTIONS_CONTRACT_REJECT option_symbol=QQQ240215C00100000 underlying=QQQ reason=delta_fail" in caplog.text
    assert "bid=1" in caplog.text
    assert "ask=1.02" in caplog.text
    assert "volume=100" in caplog.text
    assert "open_interest=500" in caplog.text
    assert "delta=0.25" in caplog.text
    assert "dte=26" in caplog.text
    assert "reject_reason=delta_failed" in caplog.text


def test_option_reject_details_are_nearest_to_underlying(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _sample_config()
    cfg["options"]["min_delta"] = 0.4
    as_of = date(2024, 1, 20)
    exp = date(2024, 2, 15)
    chain = [
        OptionContractCandidate(
            symbol="QQQ240215C00080000",
            strike=80.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.2,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00101000",
            strike=101.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.2,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00120000",
            strike=120.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.2,
        ),
    ]
    caplog.set_level("INFO")

    sel, err = select_first_ranked_candidate_within_budget(
        cfg,
        intent_underlying="QQQ",
        intent_right="call",
        chain=chain,
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=as_of,
    )

    assert sel is None
    assert err is not None
    details = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("OPTION_REJECT_DETAIL")
    ]
    assert len(details) == 3
    assert "contract=QQQ240215C00101000" in details[0]
    assert "OPTION_SCAN_SUMMARY symbol=QQQ chain_n=3 selected=0" in caplog.text
    assert "delta_failed=3" in caplog.text


def test_option_near_miss_logs_top_ten_closest_contracts(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _sample_config()
    cfg["options"]["min_delta"] = 0.4
    as_of = date(2024, 1, 20)
    exp = date(2024, 2, 15)
    chain = [
        OptionContractCandidate(
            symbol=f"QQQ240215C{int((100 + idx) * 1000):08d}",
            strike=float(100 + idx),
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.2,
        )
        for idx in range(12)
    ]
    caplog.set_level("INFO")

    sel, err = select_first_ranked_candidate_within_budget(
        cfg,
        intent_underlying="QQQ",
        intent_right="call",
        chain=chain,
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=as_of,
    )

    assert sel is None
    assert err is not None
    near_misses = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("OPTION_NEAR_MISS")
    ]
    assert len(near_misses) == 10
    assert "contract=QQQ240215C00100000" in near_misses[0]
    assert "contract=QQQ240215C00109000" in near_misses[-1]
    assert "contract=QQQ240215C00110000" not in "\n".join(near_misses)
    assert "OPTIONS_CHAIN_SUMMARY underlying=QQQ direction=call chain_size=12" in caplog.text
    assert "selected_count=0 surviving_contracts=0 top_rejection_reason=delta_failed" in caplog.text


def test_option_scan_summary_counts_unstable_quotes(caplog: pytest.LogCaptureFixture) -> None:
    cfg = _sample_config()
    cfg["options"]["contract_selection"]["max_bid_ask_spread_pct"] = 50.0
    exp = date(2024, 2, 15)
    chain = [
        OptionContractCandidate(
            symbol="QQQ240215C00100000",
            strike=100.0,
            expiration=exp,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.2,
            delta=0.45,
        ),
    ]
    caplog.set_level("INFO")

    sel, err = select_first_ranked_candidate_within_budget(
        cfg,
        intent_underlying="QQQ",
        intent_right="call",
        chain=chain,
        underlying_spot=100.0,
        equity=100_000.0,
        positions=[],
        as_of=date(2024, 1, 20),
    )

    assert sel is None
    assert err is not None
    assert "OPTION_SCAN_SUMMARY symbol=QQQ chain_n=1 selected=0" in caplog.text
    assert "stale_quote=1" in caplog.text
    assert "reject_reason=stale_quote" in caplog.text


def test_option_contract_rejection_diagnostics_include_requested_filter_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _rank_scan_config()
    cfg["options"]["min_delta"] = 0.4
    cfg["options"]["contract_selection"]["min_open_interest"] = 500
    cfg["options"]["contract_selection"]["min_volume"] = 100
    as_of = date(2024, 1, 20)
    exp_ok = date(2024, 2, 15)
    exp_late = date(2024, 4, 19)
    chain = [
        OptionContractCandidate(
            symbol="QQQ240215C00100000",
            strike=100.0,
            expiration=exp_ok,
            right="call",
            open_interest=500,
            volume=100,
            bid=0.0,
            ask=1.0,
            delta=0.45,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00101000",
            strike=101.0,
            expiration=exp_ok,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.1,
            delta=0.45,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00102000",
            strike=102.0,
            expiration=exp_ok,
            right="call",
            open_interest=100,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.45,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00103000",
            strike=103.0,
            expiration=exp_ok,
            right="call",
            open_interest=0,
            volume=50,
            bid=1.0,
            ask=1.02,
            delta=0.45,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00104000",
            strike=104.0,
            expiration=exp_ok,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.25,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00105000",
            strike=105.0,
            expiration=exp_ok,
            right="call",
            open_interest=500,
            volume=100,
            bid=4.0,
            ask=4.05,
            delta=0.45,
        ),
        OptionContractCandidate(
            symbol="QQQ240419C00100000",
            strike=100.0,
            expiration=exp_late,
            right="call",
            open_interest=500,
            volume=100,
            bid=1.0,
            ask=1.02,
            delta=0.45,
        ),
        OptionContractCandidate(
            symbol="QQQ240215C00106000",
            strike=106.0,
            expiration=exp_ok,
            right="call",
            open_interest=600,
            volume=150,
            bid=1.0,
            ask=1.02,
            delta=0.45,
        ),
    ]
    caplog.set_level("INFO")

    sel, err = select_first_ranked_candidate_within_budget(
        cfg,
        intent_underlying="QQQ",
        intent_right="call",
        chain=chain,
        underlying_spot=100.0,
        equity=10_000.0,
        positions=[],
        as_of=as_of,
    )

    assert err is None
    assert sel is not None
    assert sel.symbol == "QQQ240215C00106000"
    assert "OPTION_CHAIN_LOADED symbol=QQQ right=call chain_rows=8 path=ranked_budget" in caplog.text
    assert "OPTION_FILTER_SUMMARY symbol=QQQ chain_rows=8 selected=1" in caplog.text
    assert "OPTIONS_FUNNEL underlying=QQQ underlyings_seen=1 chains_loaded=1 contracts_examined=8" in caplog.text
    assert "contracts_after_dte=7" in caplog.text
    assert "contracts_after_delta=6" in caplog.text
    assert "contracts_after_budget=5" in caplog.text
    assert "contracts_after_spread=4" in caplog.text
    assert "contracts_after_volume=3" in caplog.text
    assert "contracts_after_open_interest=2" in caplog.text
    assert "contracts_rejected_quote=1" in caplog.text
    assert "quote_rejects=1" in caplog.text
    assert "stale_quote_rejects=0" in caplog.text
    assert "contracts_rejected_spread=1" in caplog.text
    assert "contracts_rejected_volume=1" in caplog.text
    assert "contracts_rejected_open_interest=1" in caplog.text
    assert "contracts_rejected_delta=1" in caplog.text
    assert "contracts_rejected_dte=1" in caplog.text
    assert "spread_rejects=1" in caplog.text
    assert "volume_rejects=1" in caplog.text
    assert "oi_rejects=1" in caplog.text
    assert "open_interest_rejects=1" in caplog.text
    assert "budget_rejects=1" in caplog.text
    assert "dte_rejects=1" in caplog.text
    assert "delta_rejects=1" in caplog.text
    assert "contracts_selected=1" in caplog.text
    assert "quote_fail=1" in caplog.text
    assert "spread_fail=1" in caplog.text
    assert "volume_fail=1" in caplog.text
    assert "open_interest_fail=1" in caplog.text
    assert "delta_fail=1" in caplog.text
    assert "expiry_fail=1" in caplog.text
    assert "budget_fail=1" in caplog.text
    assert "liquidity_fail=0" in caplog.text
    assert "OPTION_BEST_REJECTED symbol=QQQ" in caplog.text
    assert "OPTIONS_CHAIN_SUMMARY underlying=QQQ direction=call chain_size=8 spot_price=100 dte_range_used=14-35" in caplog.text
    assert "selected_count=1 surviving_contracts=2 top_rejection_reason=" in caplog.text
    assert "chains_loaded=1 contracts_examined=8" in caplog.text
    assert "OPTION_CANDIDATE_REJECT underlying=QQQ contract=QQQ240215C00100000" in caplog.text
    assert "OPTION_NEAR_MISS underlying=QQQ contract=QQQ240215C00100000" in caplog.text
    assert "estimated_cost=0.00" in caplog.text
    assert "OPTION_SELECTED symbol=QQQ right=call contract=QQQ240215C00106000" in caplog.text
    assert "underlying=QQQ" in caplog.text
    assert "option_symbol=QQQ240215C00106000" in caplog.text
    assert "expiration=2024-02-15" in caplog.text
    assert "call_put=call" in caplog.text
    assert "premium=101.00" in caplog.text
    assert "ranking_score=" in caplog.text
    assert "selected_reason=" in caplog.text
