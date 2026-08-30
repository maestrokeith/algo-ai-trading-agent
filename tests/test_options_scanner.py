"""Tests for scan-only options chain diagnostics."""

from __future__ import annotations

import logging
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

from src.brokers.alpaca_client import OptionContractCandidate
from src.live.options_scanner import (
    options_scan_only_active,
    scan_dynamic_candidates_option_chains,
)


class _Broker:
    def __init__(self, chain, snapshots=None, paper=True):
        self.chain = chain
        self.snapshots = snapshots or {}
        self.paper = paper
        self.calls = []

    def get_option_chain_candidates(self, underlying, *, expiration_date_gte, expiration_date_lte):
        self.calls.append((underlying, expiration_date_gte, expiration_date_lte))
        return list(self.chain)

    def get_snapshot(self, underlying):
        return dict(self.snapshots.get(str(underlying).upper(), {}))


def _config() -> dict:
    return {
        "options": {
            "enabled": True,
            "mode": "scan_only",
            "max_bid_ask_spread_pct": 5.0,
            "contract_selection": {
                "expiry_min_days": 7,
                "expiry_max_days": 21,
                "max_bid_ask_spread_pct": 5.0,
                "min_open_interest": 0,
                "min_volume": 0,
            },
        }
    }


def test_options_scan_only_active() -> None:
    assert options_scan_only_active(_config()) is True
    assert options_scan_only_active({"options": {"enabled": False, "mode": "long_premium_only"}}) is False


def test_scan_dynamic_candidates_option_chains_disabled_in_non_paper_mode(caplog) -> None:
    caplog.set_level(logging.INFO)
    broker = _Broker([], paper=False)

    results = scan_dynamic_candidates_option_chains(
        broker,
        _config(),
        [SimpleNamespace(symbol="HPE", price=20.0, score=9.5)],
        log_dt=datetime(2026, 6, 5, 9, 45, tzinfo=timezone.utc),
    )

    assert results == []
    assert broker.calls == []
    assert "OPTIONS_DISABLED_NON_PAPER_MODE" in caplog.text


def test_scan_dynamic_candidates_option_chains_logs_best_call_and_put(caplog) -> None:
    exp = date(2026, 6, 19)
    chain = [
        OptionContractCandidate(
            symbol="HPE260619C00020000",
            strike=20.0,
            expiration=exp,
            right="call",
            open_interest=1500,
            volume=800,
            bid=1.00,
            ask=1.03,
        ),
        OptionContractCandidate(
            symbol="HPE260619P00020000",
            strike=20.0,
            expiration=exp,
            right="put",
            open_interest=1400,
            volume=700,
            bid=0.95,
            ask=0.98,
        ),
    ]
    broker = _Broker(chain)
    caplog.set_level(logging.INFO)
    results = scan_dynamic_candidates_option_chains(
        broker,
        _config(),
        [SimpleNamespace(symbol="HPE", price=20.0, score=9.5)],
        log_dt=datetime(2026, 6, 5, 9, 45, tzinfo=timezone.utc),
        top_n=3,
    )

    assert len(broker.calls) == 1
    assert len(results) == 2
    assert all(result.selected is not None for result in results)
    assert "OPTIONS_SCAN_START symbol=HPE spot=20.00 chain_rows=2 mode=scan_only" in caplog.text
    assert "OPTIONS_SCAN_RESULT symbol=HPE right=call selected=HPE260619C00020000" in caplog.text
    assert "OPTIONS_SCAN_RESULT symbol=HPE right=put selected=HPE260619P00020000" in caplog.text


def test_scan_dynamic_candidates_option_chains_uses_premarket_rankings(tmp_path, caplog) -> None:
    exp = date(2026, 6, 19)
    chain = [
        OptionContractCandidate(
            symbol="AAPL260619C00200000",
            strike=200.0,
            expiration=exp,
            right="call",
            open_interest=1500,
            volume=800,
            bid=4.00,
            ask=4.10,
        ),
        OptionContractCandidate(
            symbol="MSFT260619C00400000",
            strike=400.0,
            expiration=exp,
            right="call",
            open_interest=1500,
            volume=800,
            bid=5.00,
            ask=5.10,
        ),
    ]
    broker = _Broker(chain, snapshots={"AAPL": {"price": 200.0}, "MSFT": {"price": 400.0}})
    rank_path = tmp_path / "data" / "premarket" / "latest_rankings.json"
    rank_path.parent.mkdir(parents=True)
    rank_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-06-05T05:00:00-04:00",
                "items": [
                    {"symbol": "MSFT", "score": 9.5, "catalyst_type": "analyst"},
                    {"symbol": "AAPL", "score": 8.0, "catalyst_type": "earnings"},
                    {"symbol": "AMD", "score": 7.5, "catalyst_type": "ai"},
                    {"symbol": "TSLA", "score": 6.9, "catalyst_type": "deal"},
                    {"symbol": "SPY", "score": 9.9, "catalyst_type": "sec_filing"},
                ],
            }
        )
    )
    caplog.set_level(logging.INFO)

    results = scan_dynamic_candidates_option_chains(
        broker,
        _config(),
        [SimpleNamespace(symbol="HPE", price=20.0, score=9.5)],
        log_dt=datetime(2026, 6, 5, 9, 45, tzinfo=timezone.utc),
        top_n=2,
        project_root=tmp_path,
    )

    assert [call[0] for call in broker.calls] == ["AAPL", "MSFT"]
    assert len(results) == 4
    assert "OPTIONS_CANDIDATE_FROM_PREMARKET symbol=AAPL rank_score=8.00 catalyst_type=earnings" in caplog.text
    assert "OPTIONS_CANDIDATE_FROM_PREMARKET symbol=MSFT rank_score=9.50 catalyst_type=analyst" in caplog.text
    assert "OPTIONS_CANDIDATE_FROM_PREMARKET symbol=AMD" not in caplog.text
    assert "OPTIONS_CANDIDATE_FROM_PREMARKET symbol=TSLA" not in caplog.text


def test_scan_dynamic_candidates_prioritizes_strongest_news_opportunities(caplog) -> None:
    exp = date(2026, 6, 19)
    chain = [
        OptionContractCandidate(
            symbol="NVDA260619C00100000",
            strike=100.0,
            expiration=exp,
            right="call",
            open_interest=1500,
            volume=800,
            bid=1.00,
            ask=1.03,
        )
    ]
    broker = _Broker(
        chain,
        snapshots={
            "LOW": {"price": 100.0},
            "NVDA": {"price": 100.0},
            "MID": {"price": 100.0},
        },
    )
    caplog.set_level(logging.INFO)

    scan_dynamic_candidates_option_chains(
        broker,
        _config(),
        [
            SimpleNamespace(symbol="LOW", price=100.0, score=9.9, news_score=0.0, event_score=0.0),
            SimpleNamespace(symbol="NVDA", price=100.0, score=7.0, news_score=9.0, event_score=8.0, catalyst_type="ai"),
            SimpleNamespace(symbol="MID", price=100.0, score=8.0, news_score=4.0, event_score=3.0),
        ],
        log_dt=datetime(2026, 6, 5, 9, 45, tzinfo=timezone.utc),
        top_n=2,
    )

    assert [call[0] for call in broker.calls] == ["NVDA", "MID"]
    assert "OPTIONS_CATALYST_PRIORITY symbol=NVDA" in caplog.text


def test_scan_dynamic_candidates_option_chains_is_noop_when_not_scan_only(caplog) -> None:
    broker = _Broker([])
    caplog.set_level(logging.INFO)
    results = scan_dynamic_candidates_option_chains(
        broker,
        {"options": {"enabled": False, "mode": "long_premium_only"}},
        [SimpleNamespace(symbol="HPE", price=20.0, score=9.5)],
        log_dt=datetime(2026, 6, 5, 9, 45, tzinfo=timezone.utc),
    )
    assert results == []
    assert broker.calls == []
