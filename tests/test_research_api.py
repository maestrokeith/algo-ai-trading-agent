import asyncio

import pytest
from fastapi import HTTPException

from src.api.routers.research import DemoRequest, _build_config, demo_backtest, research_status


def test_research_status_is_paper_only():
    status = asyncio.run(research_status())
    assert status["paper_only"] is True
    assert status["live_execution"] is False
    assert "XAUUSD" in status["instruments"]
    assert len(status["modules"]) == 7


def test_demo_backtest_runs_without_broker_execution():
    result = asyncio.run(
        demo_backtest(
            DemoRequest(
                symbol="EURUSD",
                bars=3500,
                seed=11,
                monte_carlo_simulations=10,
            )
        )
    )
    assert result["mode"] == "paper"
    assert result["live_execution"] is False
    assert result["symbol"] == "EURUSD"
    assert result["bars"] == 3500
    assert result["monte_carlo"]["simulations"] == 10
    assert len(result["walk_forward"]) > 0


def test_research_config_rejects_risk_above_one_percent():
    with pytest.raises(HTTPException):
        _build_config({"risk_fraction": 0.02, "max_risk_fraction": 0.02})
