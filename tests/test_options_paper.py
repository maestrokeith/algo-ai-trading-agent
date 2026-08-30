"""Tests for paper-only options entry helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.live.options_paper import (
    attempt_paper_option_entry,
    live_pilot_options_active,
    paper_only_options_active,
    paper_only_relaxed_options_config,
)


def _config() -> dict:
    return {
        "options": {
            "enabled": True,
            "allow_new_entries": True,
            "new_entries_enabled": True,
            "mode": "paper_only",
            "allowed_underlyings": ["HPE"],
            "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
            "max_daily_loss_pct": 1.0,
        }
    }


class _Broker:
    def __init__(self, *, paper: bool = True) -> None:
        self.paper = paper
        self.snapshots = []

    def get_account_snapshot(self):
        self.snapshots.append(True)
        return {"equity": 100_000.0, "last_equity": 100_500.0}


def test_paper_only_options_active() -> None:
    assert paper_only_options_active(_config()) is True
    assert paper_only_options_active({"options": {"enabled": True, "mode": "paper_only", "new_entries_enabled": False}}) is False
    assert paper_only_options_active({"options": {"enabled": True, "mode": "scan_only"}}) is False
    live_cfg = _config()
    live_cfg["options"].update({"mode": "live_long_premium", "live_pilot": {"enabled": True}})
    assert live_pilot_options_active(live_cfg, _Broker(paper=False)) is True
    assert live_pilot_options_active(live_cfg, _Broker(paper=True)) is False


def test_paper_only_relaxed_options_config_keeps_one_contract() -> None:
    cfg = _config()
    cfg["options"]["max_bid_ask_spread_pct"] = 8.0
    cfg["options"]["min_regime_score_for_entries"] = 4
    cfg["options"]["contract_selection"] = {"max_bid_ask_spread_pct": 8.0}
    cfg["options"]["dynamic_entry"] = {
        "paper_spread_relaxation": {
            "enabled": True,
            "max_bid_ask_spread_pct": 12.0,
        }
    }

    default_profile = paper_only_relaxed_options_config(cfg, dynamic_eligible=False)
    assert default_profile["options"]["max_bid_ask_spread_pct"] == pytest.approx(8.0)
    assert default_profile["options"]["contract_selection"]["max_bid_ask_spread_pct"] == pytest.approx(8.0)

    relaxed = paper_only_relaxed_options_config(cfg, dynamic_eligible=True, broker_is_paper=True)

    opts = relaxed["options"]
    assert opts["max_bid_ask_spread_pct"] == pytest.approx(12.0)
    assert opts["contract_selection"]["max_bid_ask_spread_pct"] == pytest.approx(12.0)
    assert opts["min_regime_score_for_entries"] == 2
    assert opts["max_contracts_per_trade"] == 1

    live_profile = paper_only_relaxed_options_config(cfg, dynamic_eligible=True, broker_is_paper=False)
    assert live_profile["options"]["max_bid_ask_spread_pct"] == pytest.approx(8.0)


def test_attempt_paper_option_entry_uses_news_boost_and_vwap(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = _Broker()
    captured = {}

    def fake_route(config, signal, **kwargs):  # type: ignore[no-untyped-def]
        captured["signal"] = signal
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr("src.live.options_paper.route_to_options_executor", fake_route)

    result = attempt_paper_option_entry(
        _config(),
        broker=broker,
        execution_manager=SimpleNamespace(),
        symbol="HPE",
        dt=datetime(2026, 6, 2, 9, 45, tzinfo=timezone.utc),
        current_price=20.0,
        session_vwap=19.0,
        account_equity=100_000.0,
        positions=[],
        source="dynamic_universe",
        conviction_score=0.8,
        scanner_score=51.0,
        news_score=4.0,
        event_score=2.5,
        catalyst_score=0.2,
        chain_candidates=[SimpleNamespace(symbol="HPE260619C00020000")],
    )

    assert result.placed is True
    assert result.right == "call"
    assert captured["signal"].underlying == "HPE"
    assert captured["signal"].conviction_score == pytest.approx(4.0)
    assert captured["kwargs"]["underlying_spot"] == pytest.approx(20.0)
    assert "boosted" in result.reason_codes


def test_attempt_paper_option_entry_skips_weak_dynamic_before_chain(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    def fake_chain(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("weak dynamic signal should not fetch option chain")

    def fake_route(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("weak dynamic signal should not route options")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr("src.live.options_paper.option_chain_for_underlying", fake_chain)
    monkeypatch.setattr("src.live.options_paper.route_to_options_executor", fake_route)

    result = attempt_paper_option_entry(
        _config(),
        broker=_Broker(),
        execution_manager=SimpleNamespace(),
        symbol="HPE",
        dt=datetime(2026, 6, 2, 9, 45, tzinfo=timezone.utc),
        current_price=20.0,
        session_vwap=19.0,
        account_equity=100_000.0,
        positions=[],
        source="dynamic_universe",
        scanner_score=49.0,
        news_score=7.5,
        catalyst_score=0.69,
    )

    assert result.placed is False
    assert result.reason_codes == ("dynamic_options_weak_signal",)
    assert "OPTIONS_DYNAMIC_ELIGIBILITY symbol=HPE eligible=false" in caplog.text


def test_attempt_paper_option_entry_strong_catalyst_evaluates_options(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def fake_chain(*args, **kwargs):  # type: ignore[no-untyped-def]
        captured["chain_called"] = True
        return [SimpleNamespace(symbol="HPE260619C00020000")]

    def fake_route(config, signal, **kwargs):  # type: ignore[no-untyped-def]
        captured["config"] = config
        captured["signal"] = signal
        return True

    cfg = _config()
    cfg["options"]["max_bid_ask_spread_pct"] = 8.0
    cfg["options"]["dynamic_entry"] = {
        "paper_spread_relaxation": {
            "enabled": True,
            "max_bid_ask_spread_pct": 12.0,
        }
    }
    monkeypatch.setattr("src.live.options_paper.option_chain_for_underlying", fake_chain)
    monkeypatch.setattr("src.live.options_paper.route_to_options_executor", fake_route)

    result = attempt_paper_option_entry(
        cfg,
        broker=_Broker(),
        execution_manager=SimpleNamespace(),
        symbol="HPE",
        dt=datetime(2026, 6, 2, 9, 45, tzinfo=timezone.utc),
        current_price=20.0,
        session_vwap=19.0,
        account_equity=100_000.0,
        positions=[],
        source="dynamic_universe",
        scanner_score=20.0,
        news_score=4.0,
        catalyst_score=0.72,
    )

    assert result.placed is True
    assert captured["chain_called"] is True
    assert captured["config"]["options"]["max_bid_ask_spread_pct"] == pytest.approx(12.0)
    assert "catalyst_score" in result.reason_codes


def test_attempt_paper_option_entry_disabled_in_non_paper_mode(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    def fake_route(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("options route should not run outside paper mode")

    caplog.set_level(logging.INFO)
    monkeypatch.setattr("src.live.options_paper.route_to_options_executor", fake_route)

    result = attempt_paper_option_entry(
        _config(),
        broker=_Broker(paper=False),
        execution_manager=SimpleNamespace(),
        symbol="HPE",
        dt=datetime(2026, 6, 2, 9, 45, tzinfo=timezone.utc),
        current_price=20.0,
        session_vwap=19.0,
        account_equity=100_000.0,
        positions=[],
        source="dynamic_universe",
        scanner_score=60.0,
        news_score=9.0,
        catalyst_score=0.8,
        chain_candidates=[SimpleNamespace(symbol="HPE260619C00020000")],
    )

    assert result.placed is False
    assert result.reason_codes == ("non_paper_mode",)
    assert "OPTIONS_DISABLED_NON_PAPER_MODE" in caplog.text


def test_attempt_option_entry_runs_live_pilot_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config()
    cfg["options"].update(
        {
            "mode": "live_long_premium",
            "live_pilot": {"enabled": True},
            "live_pilot_enabled": True,
            "total_exposure_limit": 0.01,
            "max_contracts_per_trade": 1,
            "max_positions": 1,
        }
    )
    captured = {}

    def fake_route(config, signal, **kwargs):  # type: ignore[no-untyped-def]
        captured["config"] = config
        captured["signal"] = signal
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr("src.live.options_paper.route_to_options_executor", fake_route)

    result = attempt_paper_option_entry(
        cfg,
        broker=_Broker(paper=False),
        execution_manager=SimpleNamespace(),
        symbol="HPE",
        dt=datetime(2026, 6, 2, 9, 45, tzinfo=timezone.utc),
        current_price=20.0,
        session_vwap=19.0,
        account_equity=100_000.0,
        positions=[],
        source="dynamic_universe",
        scanner_score=60.0,
        news_score=9.0,
        catalyst_score=0.8,
        chain_candidates=[SimpleNamespace(symbol="HPE260619C00020000")],
    )

    assert result.placed is True
    assert result.reason_codes[0] == "live_pilot"
    assert captured["config"]["options"]["mode"] == "live_long_premium"
