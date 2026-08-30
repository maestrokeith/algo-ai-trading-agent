"""Tests for automated daily trading report data collection."""

from __future__ import annotations

from datetime import date

from src.daily_trading_report import collect_daily_trading_report_data, normalize_daily_trade


class StubBroker:
    def get_account_snapshot(self) -> dict[str, float]:
        return {"equity": 101_000.0, "last_equity": 100_000.0, "cash": 25_000.0}

    def get_positions(self) -> list[dict[str, object]]:
        return [{"symbol": "AAPL", "market_value": 10_000.0, "side": "long"}]

    def get_orders_for_date(self, trade_date: date) -> list[dict[str, object]]:
        return [
            {
                "id": "core-1",
                "symbol": "AAPL",
                "side": "sell",
                "qty": 2,
                "filled_avg_price": 200.0,
                "pnl": 120.0,
            },
            {
                "id": "dynamic-1",
                "symbol": "CRWD",
                "side": "sell",
                "qty": 1,
                "filled_avg_price": 300.0,
                "realized_pnl": -40.0,
                "catalyst_type": "ai",
                "news_score": 8,
            },
        ]

    def get_portfolio_equity_series(self) -> dict[str, list[float | str]]:
        return {"dates": ["2026-06-04", "2026-06-05"], "equity": [100_000.0, 101_000.0]}


def test_collect_daily_trading_report_data_uses_orders_and_snapshot_pnl() -> None:
    data = collect_daily_trading_report_data(
        broker=StubBroker(),
        config={
            "universe": {"symbols": ["AAPL", "MSFT"]},
            "portfolio": {"total_contributed_usd": 90_000.0},
        },
        trade_date=date(2026, 6, 5),
    )

    assert data.account["equity"] == 101_000.0
    assert data.account["pnl_today"] == 1_000.0
    assert data.account["total_contributed_usd"] == 90_000.0
    assert len(data.trades) == 2
    assert data.trades[0]["strategy"] == "manual_or_core"
    assert data.trades[1]["strategy"] == "dynamic_universe"
    assert data.trades[1]["pnl"] == -40.0
    assert data.trades[1]["return_pct"] == -40.0 / 300.0 * 100.0
    assert data.trades[1]["catalyst_type"] == "ai"
    assert data.trades[1]["news_score"] == 8.0
    assert data.exposure["gross"] == 10_000.0 / 101_000.0 * 100.0
    assert data.portfolio_history is not None


def test_normalize_daily_trade_prefers_explicit_strategy() -> None:
    trade = normalize_daily_trade(
        {"symbol": "AAPL", "qty": "1", "side": "buy", "strategy": "trend_long", "profit_loss": "5.5"},
        core_symbols={"AAPL"},
    )

    assert trade["strategy"] == "trend_long"
    assert trade["qty"] == 1.0
    assert trade["pnl"] == 5.5
    assert trade["return_pct"] is None


def test_normalize_daily_trade_carries_dynamic_performance_metadata() -> None:
    trade = normalize_daily_trade(
        {
            "symbol": "CRWD",
            "qty": "2",
            "side": "sell",
            "strategy": "dynamic_universe",
            "profit_loss": "42.5",
            "return_pct": "3.25",
            "news_score": "8",
            "catalyst_type": "deal",
        },
        core_symbols={"AAPL"},
    )

    assert trade["strategy"] == "dynamic_universe"
    assert trade["pnl"] == 42.5
    assert trade["return_pct"] == 3.25
    assert trade["news_score"] == 8.0
    assert trade["catalyst_type"] == "deal"
