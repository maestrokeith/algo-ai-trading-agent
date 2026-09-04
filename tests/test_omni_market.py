import asyncio

from engine.omni_market import (
    LIVE_EXECUTION,
    PAPER_ONLY,
    forex_sniper,
    memecoin_radar,
    options_lab,
    research_snapshot,
    scan_markets,
)
from src.api.routers.omni import omni_status


def test_omni_boundary_is_paper_only():
    assert PAPER_ONLY is True
    assert LIVE_EXECUTION is False
    status = asyncio.run(omni_status())
    assert status["paper_only"] is True
    assert status["live_execution"] is False
    assert "memecoin" in status["markets"]
    assert "futures" in status["markets"]


def test_omni_scan_covers_multiple_asset_classes():
    result = scan_markets(["forex", "futures", "crypto", "memecoin"], seed=11)
    classes = {row["asset_class"] for row in result["rows"]}
    assert {"forex", "futures", "crypto", "memecoin"}.issubset(classes)
    assert result["paper_only"] is True
    assert result["live_execution"] is False
    assert 0 <= result["leader"]["score"] <= 100


def test_forex_sniper_is_research_not_execution():
    result = forex_sniper("XAUUSD", seed=5)
    assert result["paper_only"] is True
    assert result["live_execution"] is False
    assert result["research_direction"] in {"LONG_RESEARCH", "SHORT_RESEARCH", "WAIT"}
    assert 0 <= result["sniper_score"] <= 100


def test_probabilistic_forecast_is_bounded():
    result = research_snapshot("crypto", "BTCUSDT", seed=9).as_dict()
    assert 0.05 <= result["probability_up"] <= 0.95
    assert 0 <= result["confidence"] <= 1
    assert len(result["agents"]) == 6


def test_memecoin_radar_exposes_risk_diagnostics():
    result = memecoin_radar(["DOGEUSDT", "BONKUSDT"], seed=3)
    assert len(result["rows"]) == 2
    for row in result["rows"]:
        assert 0 <= row["rug_risk"] <= 100
        assert 0 <= row["quality_score"] <= 100
        assert 0 <= row["momentum_score"] <= 100


def test_options_lab_is_theoretical_and_has_payoff_grid():
    result = options_lab("SPY", 560, 560, 30, 0.22)
    assert result["paper_only"] is True
    assert result["live_execution"] is False
    assert result["model"]["call_value"] > 0
    assert result["model"]["put_value"] > 0
    assert len(result["payoff"]) == 41
