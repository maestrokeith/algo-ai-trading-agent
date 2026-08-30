from __future__ import annotations

from src.shadow_mode import (
    compare_shadow_to_live,
    load_shadow_ledger,
    record_shadow_decision,
    record_shadow_outcome,
    shadow_mode_enabled,
)


def test_shadow_mode_enabled_global_and_strategy_allowlist() -> None:
    assert shadow_mode_enabled({"shadow_mode": {"enabled": True}}) is True
    assert shadow_mode_enabled(
        {"shadow_mode": {"enabled": True, "strategies": ["dynamic"]}}, "dynamic"
    ) is True
    assert shadow_mode_enabled(
        {"shadow_mode": {"enabled": True, "strategies": ["dynamic"]}}, "core"
    ) is False
    assert shadow_mode_enabled({"shadow_mode": {"enabled": False}}) is False


def test_record_shadow_decision_and_outcome_persist(tmp_path) -> None:
    decision = record_shadow_decision(
        strategy="dynamic",
        symbol="aapl",
        action="buy",
        confidence=0.8,
        reason="breakout",
        reference_price=200.0,
        data_dir=tmp_path,
    )
    outcome = record_shadow_outcome(
        strategy="dynamic",
        symbol="AAPL",
        pnl=12.5,
        return_pct=1.2,
        data_dir=tmp_path,
    )
    ledger = load_shadow_ledger(data_dir=tmp_path)

    assert decision.symbol == "AAPL"
    assert outcome["pnl"] == 12.5
    assert ledger["decisions"][0]["action"] == "buy"
    assert ledger["outcomes"][0]["return_pct"] == 1.2


def test_compare_shadow_to_live_reports_matches_and_shadow_pnl(tmp_path) -> None:
    record_shadow_decision(
        strategy="dynamic",
        symbol="AAPL",
        action="buy",
        data_dir=tmp_path,
    )
    record_shadow_decision(
        strategy="dynamic",
        symbol="MSFT",
        action="buy",
        data_dir=tmp_path,
    )
    record_shadow_outcome(strategy="dynamic", symbol="AAPL", pnl=5.0, data_dir=tmp_path)

    comparison = compare_shadow_to_live(
        live_decisions=[
            {"strategy": "dynamic", "symbol": "AAPL", "action": "buy"},
            {"strategy": "dynamic", "symbol": "NVDA", "action": "buy"},
        ],
        strategy="dynamic",
        data_dir=tmp_path,
    )

    assert comparison.matches == 1
    assert comparison.shadow_only == 1
    assert comparison.live_only == 1
    assert comparison.realized_pnl == 5.0
    assert comparison.as_dict()["match_rate"] == 0.5
