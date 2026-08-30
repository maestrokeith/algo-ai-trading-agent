"""Tests for safe paper options pipeline diagnostics."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import yaml

from scripts.run_paper_options_diagnostics import run_paper_options_diagnostics


def _write_project(root: Path, *, paper: bool = True, max_premium_per_trade: float | None = None) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "default.yaml").write_text(
        yaml.safe_dump(
            {
                "broker": {"paper": True},
                "strategy": {
                    "trend_following": {
                        "ma_fast": 10,
                        "ma_slow": 50,
                        "entry_mode": "momentum",
                        "volatility_filter_atr_period": 14,
                        "max_atr_pct_for_entry": 99.0,
                    },
                    "exits": {"stop_loss_pct": 2.0, "take_profit_pct": 4.0, "time_bars_exit": 20},
                },
                "execution": {
                    "allow_fractional": True,
                    "prefer_limit_orders": True,
                    "max_spread_pct": 5.0,
                    "min_trade_dollars": 0.0,
                },
                "position_sizing": {
                    "risk_per_trade_pct": 1.0,
                    "max_open_risk_pct": 100.0,
                    "max_exposure_per_symbol_pct": 100.0,
                    "volatility_sizing": {"enabled": False},
                    "portfolio_heat": {"enabled": False},
                    "high_vol_reduction": {"enabled": False},
                    "confidence_sizing": {"enabled": False},
                },
                "portfolio": {"exposure_gates": {"enabled": False}},
                "universe": {"symbols": ["QQQ"]},
                "trade_filters": {"volatility_do_not_trade": {"enabled": False}},
                "market_regime": {},
                "holidays": [],
                "options": {
                    "enabled": False,
                    "mode": "paper_only",
                    "allowed_underlyings": ["QQQ"],
                    "entry_mapping": {"bullish_signal": "call", "bearish_signal": "put"},
                    **(
                        {"max_premium_per_trade": max_premium_per_trade}
                        if max_premium_per_trade is not None
                        else {}
                    ),
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "config" / "users.yaml").write_text(
        yaml.safe_dump(
            {
                "users": [
                    {
                        "id": "paper_bot",
                        "alpaca_key_env": "APCA_API_KEY_ID",
                        "alpaca_secret_env": "APCA_API_SECRET_KEY",
                        "paper": paper,
                        "overrides": {},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_paper_options_diagnostics_runs_entry_and_option_pipeline(
    tmp_path: Path,
    caplog,
) -> None:
    _write_project(tmp_path)

    with caplog.at_level(logging.INFO):
        result = run_paper_options_diagnostics(
            project_root=tmp_path,
            user_id="paper_bot",
            symbol="QQQ",
            data_dir=tmp_path / "data" / "paper_options_diagnostics",
            now=datetime(2026, 6, 9, 14, 0, 0),
        )

    assert result["ok"] is True
    assert result["entry_allowed"] is True
    assert result["options_attempted"] is True
    stages = result["option_diagnostics"]
    assert stages["signal"]["passed"] is True
    assert stages["allowed_underlying"]["passed"] is True
    assert stages["contract_found"]["passed"] is True
    assert stages["liquidity"]["passed"] is True
    assert "sizing" in stages
    assert "risk_cap" in stages
    assert "submit_attempted" in stages
    assert "broker_response" in stages
    assert "OPTIONS_CONFIG enabled=true mode=paper_only paper_only_active=true" in caplog.text
    assert "ENTRY_PIPELINE_STAGE symbol=QQQ stage=entry_eval_start" in caplog.text
    assert "QQQ ENTRY_EVAL route=paper_options_diagnostic" in caplog.text
    assert "OPTION_PIPELINE_STAGE symbol=QQQ stage=options_route result=running" in caplog.text
    assert result["chain_source"] == "mock"
    assert "MOCK_CHAIN_USED symbol=QQQ chain_rows=4" in caplog.text
    assert "OPTIONS_DYNAMIC_ELIGIBILITY symbol=QQQ eligible=true reason=dynamic_gate_not_required" in caplog.text
    assert "dynamic_options_weak_signal" not in caplog.text
    assert "OPTION_CHAIN_LOADED symbol=QQQ right=call chain_rows=4 path=ranked_budget" in caplog.text
    assert "OPTION_FILTER_SUMMARY symbol=QQQ chain_rows=4" in caplog.text
    assert (
        "OPTION_BEST_REJECTED symbol=QQQ" in caplog.text
        or "OPTION_SELECTED symbol=QQQ" in caplog.text
    )
    assert "PAPER_OPTIONS_DIAGNOSTIC symbol=QQQ signal=True allowed_underlying=True" in caplog.text


def test_paper_options_diagnostics_selects_cheaper_contract_under_tight_budget(
    tmp_path: Path,
    caplog,
) -> None:
    _write_project(tmp_path, max_premium_per_trade=150.0)

    with caplog.at_level(logging.INFO):
        result = run_paper_options_diagnostics(
            project_root=tmp_path,
            user_id="paper_bot",
            symbol="QQQ",
            data_dir=tmp_path / "data" / "paper_options_diagnostics",
            now=datetime(2026, 6, 9, 14, 0, 0),
        )

    assert result["options_placed"] is True
    assert result["paper_mock_orders"][0]["symbol"] == "QQQ260630C00360000"
    assert "premium_over_budget=1" in caplog.text
    assert "OPTION_REJECT_DETAIL symbol=QQQ contract=QQQ260630C00350000" in caplog.text
    assert "reject_reason=premium_over_budget" in caplog.text
    assert "OPTION_SELECTED symbol=QQQ right=call contract=QQQ260630C00360000" in caplog.text


def test_paper_options_diagnostics_refuses_non_paper_user(tmp_path: Path) -> None:
    _write_project(tmp_path, paper=False)

    try:
        run_paper_options_diagnostics(
            project_root=tmp_path,
            user_id="paper_bot",
            symbol="QQQ",
            data_dir=tmp_path / "data" / "paper_options_diagnostics",
        )
    except RuntimeError as exc:
        assert "paper_options_diagnostics_requires_paper_user" in str(exc)
    else:
        raise AssertionError("expected non-paper user to be rejected")
