from src.agents.post_trade_agent import PostTradeAgent


def test_post_trade_agent_generates_lesson():
    review = PostTradeAgent().review(
        strategy="VWAP_BREAKOUT",
        regime_at_entry="CHOP",
        entry_price=100,
        exit_price=99,
        holding_time_minutes=12,
        expected_behavior="breakout",
        actual_behavior="failed",
    )

    assert review.return_pct == -1.0
    assert "Reduce confidence" in review.lesson
