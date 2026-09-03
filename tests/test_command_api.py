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


def test_live_money_command_is_blocked():
    result = asyncio.run(
        execute_command(CommandRequest(command="place live order on XAUUSD", symbol="XAUUSD"))
    )
    assert result["intent"] == "blocked"
    assert result["live_execution"] is False
    assert result["result"] is None


def test_demo_command_runs_without_live_execution():
    result = asyncio.run(
        execute_command(CommandRequest(command="backtest EURUSD", symbol="EURUSD", bars=3500, seed=9))
    )
    assert result["intent"] == "demo_backtest"
    assert result["live_execution"] is False
    assert result["result"]["mode"] == "paper"
    assert result["result"]["symbol"] == "EURUSD"


def test_autonomous_cycle_is_research_only():
    result = asyncio.run(
        autonomous_cycle(AutonomousCycleRequest(symbols=["XAUUSD"], bars=3500, seed=4))
    )
    assert result["paper_only"] is True
    assert result["live_execution"] is False
    assert len(result["symbols"]) == 1
    assert result["symbols"][0]["symbol"] == "XAUUSD"
