"""Tests for paper-only options runtime gating."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from src.brokers.alpaca_client import OptionContractCandidate
from src.entry_router import EntryRouteSignal, route_to_options_executor, should_use_options
from src.live.options_chain import (
    broker_mode,
    is_optionable_underlying_symbol,
    log_options_disabled_non_paper_once,
    option_chain_for_underlying,
    options_runtime_enabled,
    reset_options_non_paper_log_flags,
)
from src.options_config import options_ordering_allowed
from src.live.options_scanner import scan_dynamic_candidates_option_chains


class _Broker:
    def __init__(self, *, paper: bool = True, chain: list | None = None) -> None:
        self.paper = paper
        self.chain = list(chain or [])
        self.chain_calls: list[tuple] = []
        self.option_quote_calls: list[str] = []

    def get_option_chain_candidates(self, underlying, *, expiration_date_gte, expiration_date_lte):
        self.chain_calls.append((underlying, expiration_date_gte, expiration_date_lte))
        return list(self.chain)

    def get_option_latest_quote(self, symbol: str):
        self.option_quote_calls.append(str(symbol))
        return SimpleNamespace(bid=1.0, ask=1.05, mid=1.025, spread_pct=4.0)


def _config(*, enabled: bool = True) -> dict:
    return {
        "options": {
            "enabled": enabled,
            "mode": "paper_only",
            "only_buy_options": True,
            "allowed_underlyings": ["NVTS"],
            "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
            "contract_selection": {"expiry_min_days": 7, "expiry_max_days": 21},
        }
    }


def test_options_runtime_enabled_requires_paper_and_config_flag() -> None:
    cfg = _config()
    assert options_runtime_enabled(_Broker(paper=True), cfg) is True
    assert options_runtime_enabled(_Broker(paper=False), cfg) is False
    assert options_runtime_enabled(_Broker(paper=True), _config(enabled=False)) is False
    assert broker_mode(_Broker(paper=False), cfg) == "live"
    assert broker_mode(_Broker(paper=True), cfg) == "paper"


def test_live_options_pilot_disabled_blocks_live_runtime() -> None:
    cfg = {
        "options": {
            "enabled": True,
            "mode": "live",
            "live_pilot_enabled": False,
        }
    }

    assert options_runtime_enabled(_Broker(paper=False), cfg) is False
    assert options_ordering_allowed(cfg, broker_is_paper=False) == (
        False,
        "live options not explicitly enabled",
    )


def test_live_options_pilot_enabled_allows_live_runtime() -> None:
    cfg = {
        "options": {
            "enabled": True,
            "mode": "live",
            "live_pilot_enabled": True,
            "max_option_positions": 1,
            "max_contracts_per_trade": 1,
            "total_exposure_limit": 0.01,
            "per_trade": 0.005,
            "require_top_signal": True,
            "never_bypass_stock_risk_caps": True,
        }
    }

    assert options_runtime_enabled(_Broker(paper=False), cfg) is True
    assert options_ordering_allowed(cfg, broker_is_paper=False) == (True, None)


def test_live_options_nested_pilot_enabled_allows_live_long_premium_runtime() -> None:
    cfg = {
        "options": {
            "enabled": True,
            "mode": "live_long_premium",
            "live_pilot": {"enabled": True},
            "max_option_positions": 1,
            "max_contracts_per_trade": 1,
            "total_exposure_limit": 0.01,
            "per_trade": 0.005,
            "require_top_signal": True,
            "never_bypass_stock_risk_caps": True,
        }
    }

    assert options_runtime_enabled(_Broker(paper=False), cfg) is True
    assert options_ordering_allowed(cfg, broker_is_paper=False) == (True, None)


def test_live_mode_never_fetches_option_chain(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    broker = _Broker(paper=False, chain=[SimpleNamespace(symbol="NVTS260619C00010000")])
    out = option_chain_for_underlying(
        broker,
        _config(),
        "NVTS",
        datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc),
    )
    assert out == []
    assert broker.chain_calls == []


def test_paper_mode_fetches_option_chain_when_enabled() -> None:
    broker = _Broker(
        paper=True,
        chain=[
            OptionContractCandidate(
                symbol="NVTS260619C00010000",
                strike=10.0,
                expiration=date(2026, 6, 19),
                right="call",
                open_interest=0,
                volume=100,
                bid=1.0,
                ask=1.05,
            )
        ],
    )
    out = option_chain_for_underlying(
        broker,
        _config(),
        "NVTS",
        datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc),
    )
    assert len(out) == 1
    assert len(broker.chain_calls) == 1


def test_non_optionable_synthetic_underlying_skips_option_chain_lookup(caplog: pytest.LogCaptureFixture) -> None:
    broker = _Broker(
        paper=True,
        chain=[
            OptionContractCandidate(
                symbol="BTCUSD260619C00100000",
                strike=100.0,
                expiration=date(2026, 6, 19),
                right="call",
                open_interest=100,
                volume=100,
                bid=1.0,
                ask=1.1,
            )
        ],
    )
    caplog.set_level(logging.INFO)

    out = option_chain_for_underlying(
        broker,
        _config(),
        "BTCUSD",
        datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc),
    )

    assert out == []
    assert broker.chain_calls == []
    assert is_optionable_underlying_symbol("BTCUSD") is False
    assert is_optionable_underlying_symbol("NVDA") is True
    assert "OPTIONS_SKIP symbol=BTCUSD reason=not_optionable_underlying" in caplog.text


def test_scan_dynamic_option_chains_skipped_in_live_mode(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    broker = _Broker(paper=False)
    results = scan_dynamic_candidates_option_chains(
        broker,
        {**_config(), "options": {**_config()["options"], "mode": "scan_only"}},
        [SimpleNamespace(symbol="NVTS", price=5.0, score=9.0)],
        log_dt=datetime(2026, 6, 3, 14, 0, tzinfo=timezone.utc),
    )
    assert results == []
    assert broker.chain_calls == []
    assert "OPTIONS_DISABLED_NON_PAPER_MODE" in caplog.text


def test_should_use_options_false_in_live_mode() -> None:
    signal = EntryRouteSignal(
        underlying="NVTS",
        direction="bullish",
        source="trend_long",
        conviction_score=0.9,
    )
    assert should_use_options(_config(), signal, broker=_Broker(paper=False)) is False


def test_route_to_options_executor_noop_in_live_mode(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    signal = EntryRouteSignal(underlying="NVTS", direction="bullish", source="trend_long")
    placed = route_to_options_executor(
        _config(),
        signal,
        broker=_Broker(paper=False),
        chain_candidates=[SimpleNamespace(symbol="NVTS260619C00010000")],
        account_equity=100_000.0,
        positions=[],
    )
    assert placed is False
    assert "OPTIONS_DISABLED_NON_PAPER_MODE" in caplog.text


def test_log_options_disabled_non_paper_once_per_user(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    reset_options_non_paper_log_flags()
    broker = _Broker(paper=False)
    cfg = _config()
    log_options_disabled_non_paper_once("u1", broker, cfg)
    log_options_disabled_non_paper_once("u1", broker, cfg)
    log_options_disabled_non_paper_once("u2", broker, cfg)
    assert caplog.text.count("OPTIONS_DISABLED_NON_PAPER_MODE") == 2


def test_alpaca_broker_skips_option_latest_quote_in_live_mode() -> None:
    from src.brokers.alpaca_client import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.paper = False
    broker._option_data = object()
    assert broker.get_option_latest_quote("NVTS260619C00010000") is None
