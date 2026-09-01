from datetime import datetime, timezone

from src.agents.critic_agent import CriticAgent
from src.intelligence.schemas import MarketContext, Regime, RegimeAssessment, TradeProposal, TradeSide


def test_critic_rejects_extended_entry():
    market = MarketContext(
        symbol="TSLA",
        timestamp=datetime.now(timezone.utc),
        last_price=250,
        spread_pct=0.1,
        vwap=240,
        distance_from_vwap_pct=4.0,
    )
    proposal = TradeProposal(
        "tp-1",
        "TSLA",
        TradeSide.BUY,
        "VWAP_BREAKOUT",
        "limit",
        250,
        245,
        260,
        30,
        0.9,
        "test",
        (),
        (),
        datetime.now(timezone.utc),
    )

    review = CriticAgent().critique(proposal, market, RegimeAssessment(Regime.TREND_UP, 0.8))

    assert not review.approved
    assert "extended" in review.rejection_reasons[0]
