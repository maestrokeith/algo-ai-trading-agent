from __future__ import annotations

from src.global_risk import evaluate_global_risk, get_kill_switch, set_kill_switch


def _config() -> dict:
    return {
        "global_risk": {
            "max_daily_loss_pct": 0.03,
            "max_drawdown_pct": 0.10,
            "consecutive_loss_lockout": 3,
            "max_symbol_exposure_pct": 0.25,
            "max_sector_exposure_pct": 0.40,
            "max_options_exposure_pct": 0.10,
        }
    }


def test_global_risk_allows_clean_account(tmp_path) -> None:
    status = evaluate_global_risk(
        equity=100_000,
        peak_equity=101_000,
        last_equity=100_500,
        positions=[{"symbol": "SPY", "market_value": 10_000}],
        trades=[{"pnl": -5}, {"pnl": 20}],
        config=_config(),
        data_dir=tmp_path,
    )

    assert status.allowed is True
    assert status.alerts == []
    assert status.metrics["daily_pnl_pct"] < 0


def test_global_risk_blocks_daily_loss_drawdown_and_consecutive_losses(tmp_path) -> None:
    status = evaluate_global_risk(
        equity=88_000,
        peak_equity=100_000,
        last_equity=92_000,
        trades=[{"pnl": -1}, {"pnl": -2}, {"pnl": -3}],
        config=_config(),
        data_dir=tmp_path,
    )

    assert status.allowed is False
    codes = {alert.code for alert in status.blocking_alerts}
    assert {"daily_loss_limit", "max_drawdown", "consecutive_loss_lockout"} <= codes


def test_global_risk_blocks_symbol_sector_and_options_exposure(tmp_path) -> None:
    positions = [
        {"symbol": "NVDA", "market_value": 30_000, "sector": "technology"},
        {"symbol": "MSFT", "market_value": 12_000, "sector": "technology"},
        {
            "symbol": "AAPL260620C00200000",
            "market_value": 11_000,
            "asset_class": "option",
            "sector": "technology",
        },
    ]

    status = evaluate_global_risk(
        equity=100_000,
        peak_equity=100_000,
        last_equity=100_000,
        positions=positions,
        config=_config(),
        data_dir=tmp_path,
    )

    assert status.allowed is False
    codes = [alert.code for alert in status.blocking_alerts]
    assert "max_symbol_exposure" in codes
    assert "max_sector_exposure" in codes
    assert "max_options_exposure" in codes
    assert status.metrics["symbol_exposure_pct"]["NVDA"] == 30.0


def test_kill_switch_blocks_until_cleared(tmp_path) -> None:
    set_kill_switch(True, user_id="u1", reason="operator pause", data_dir=tmp_path)
    assert get_kill_switch(user_id="u1", data_dir=tmp_path) == (True, "operator pause")

    blocked = evaluate_global_risk(
        equity=100_000,
        config=_config(),
        user_id="u1",
        data_dir=tmp_path,
    )
    assert blocked.allowed is False
    assert blocked.kill_switch_active is True
    assert blocked.blocking_alerts[0].code == "kill_switch"

    set_kill_switch(False, user_id="u1", data_dir=tmp_path)
    allowed = evaluate_global_risk(
        equity=100_000,
        config=_config(),
        user_id="u1",
        data_dir=tmp_path,
    )
    assert allowed.allowed is True
    assert allowed.kill_switch_active is False
