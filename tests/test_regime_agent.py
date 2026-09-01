from datetime import datetime, timezone

from src.agents.regime_agent import RegimeAgent
from src.intelligence.schemas import MarketContext, Regime


def test_regime_agent_detects_trend_up():
    ctx = MarketContext(
        symbol="NVDA",
        timestamp=datetime.now(timezone.utc),
        last_price=101,
        vwap=100,
        distance_from_vwap_pct=1.0,
        recent_returns=(0.01, 0.002, -0.001, 0.003, 0.004),
        volatility=0.006,
        relative_volume=1.8,
    )

    out = RegimeAgent().assess(ctx)

    assert out.regime == Regime.TREND_UP
    assert out.confidence > 0
    assert out.evidence
