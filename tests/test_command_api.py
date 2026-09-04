import asyncio

from src.api.routers.command import (
    AutonomousCycleRequest,
    CommandRequest,
    autonomous_cycle,
    command_status,
    execute_command,
)


def test_command_status_is_paper_only():
    status = asyncio.run(command_status())
    assert status["paper_only"] is True
    assert status["live_execution"] is False
    assert status["autonomy"] == "bounded_research_only"
    assert "omni-market scan" in status["capabilities"]


def test_live_money_command_is_blocked():
    result = asyncio.run(execute_command(CommandRequest(command="place live order on XAUUSD", symbol="XAUUSD")))
    assert result["intent"] == "blocked"
    assert result["live_execution"] is False
    assert result["result"] is None


def test_demo_command_runs_without_live_execution():
    result = asyncio.run(execute_command(CommandRequest(command="backtest EURUSD", symbol="EURUSD", bars=3500, seed=9)))
    assert result["intent"] == "demo_backtest"
    assert result["live_execution"] is False
    assert result["result"]["mode"] == "paper"
    assert result["result"]["symbol"] == "EURUSD"


def test_autonomous_cycle_is_research_only():
    result = asyncio.run(autonomous_cycle(AutonomousCycleRequest(symbols=["XAUUSD"], bars=3500, seed=4)))
    assert result["paper_only"] is True
    assert result["live_execution"] is False
    assert len(result["symbols"]) == 1
    assert result["symbols"][0]["symbol"] == "XAUUSD"


def test_scan_everything_routes_to_omni_research():
    result = asyncio.run(execute_command(CommandRequest(command="scan everything", seed=12)))
    assert result["intent"] == "omni_market_scan"
    assert result["live_execution"] is False
    classes = {row["asset_class"] for row in result["result"]["rows"]}
    assert {"forex", "futures", "crypto", "memecoin"}.issubset(classes)


def test_memecoin_command_routes_to_risk_radar():
    result = asyncio.run(execute_command(CommandRequest(command="memecoin radar", seed=8)))
    assert result["intent"] == "memecoin_radar"
    assert result["live_execution"] is False
    assert len(result["result"]["rows"]) >= 4


def test_options_command_is_theoretical_only():
    result = asyncio.run(execute_command(CommandRequest(command="BTC options research", seed=8)))
    assert result["intent"] == "options_lab"
    assert result["live_execution"] is False
    assert result["result"]["paper_only"] is True
    assert result["result"]["underlying"] == "BTC"


def test_prediction_command_is_probabilistic_research():
    result = asyncio.run(execute_command(CommandRequest(command="predict bitcoin", seed=8)))
    assert result["intent"] == "probabilistic_forecast"
    assert result["live_execution"] is False
    assert 0.05 <= result["result"]["probability_up"] <= 0.95
