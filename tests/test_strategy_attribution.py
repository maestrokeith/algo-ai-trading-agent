from __future__ import annotations

from src.strategy_attribution import (
    build_strategy_attribution,
    classify_trade_bucket,
    strategy_attribution_dashboard,
)


def test_classify_trade_bucket_required_categories() -> None:
    assert classify_trade_bucket({"symbol": "AAPL", "strategy": "core"}) == "core"
    assert classify_trade_bucket({"symbol": "MSFT", "strategy": "dynamic"}) == "dynamic"
    assert classify_trade_bucket({"symbol": "AAPL260620C00200000"}) == "options"
    assert classify_trade_bucket({"symbol": "NVDA", "catalyst_type": "earnings"}) == "news-driven"
    assert classify_trade_bucket({"symbol": "SPY", "strategy": "fallback"}) == "ETF fallback"


def test_build_strategy_attribution_tracks_pnl_separately() -> None:
    rows = build_strategy_attribution(
        [
            {"symbol": "AAPL", "strategy": "core", "pnl": 10},
            {"symbol": "MSFT", "strategy": "dynamic", "pnl": -5},
            {"symbol": "AAPL260620C00200000", "pnl": 7},
            {"symbol": "NVDA", "catalyst_type": "earnings", "pnl": 3},
            {"symbol": "SPY", "route": "etf_fallback", "pnl": -2},
        ]
    )

    by_bucket = {row.bucket: row for row in rows}
    assert by_bucket["core"].realized_pnl == 10
    assert by_bucket["dynamic"].losses == 1
    assert by_bucket["options"].wins == 1
    assert by_bucket["news-driven"].win_rate == 1.0
    assert by_bucket["ETF fallback"].avg_pnl == -2


def test_strategy_attribution_dashboard_totals() -> None:
    dashboard = strategy_attribution_dashboard(
        [
            {"symbol": "AAPL", "strategy": "core", "pnl": 10},
            {"symbol": "MSFT", "strategy": "dynamic", "pnl": -5},
        ]
    )

    assert dashboard["total_pnl"] == 5
    assert dashboard["total_trades"] == 2
    assert len(dashboard["rows"]) == 5
