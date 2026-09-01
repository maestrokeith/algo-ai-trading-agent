from src.agents.post_trade_agent import PostTradeAgent
from src.intelligence.trade_memory import TradeMemory


def test_trade_memory_updates_strategy_statistics(tmp_path):
    memory = TradeMemory(tmp_path / "memory.db")
    review = PostTradeAgent().review(
        strategy="VWAP_BREAKOUT",
        regime_at_entry="TREND_UP",
        entry_price=100,
        exit_price=102,
        holding_time_minutes=20,
        expected_behavior="breakout",
        actual_behavior="followed through",
    )

    memory.record_post_trade_review(review)

    stats = memory.strategy_stats()
    assert stats[0].strategy == "VWAP_BREAKOUT"
    assert stats[0].trades == 1
    assert stats[0].avg_return == 2.0
