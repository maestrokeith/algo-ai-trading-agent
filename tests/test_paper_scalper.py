import numpy as np
import pandas as pd

from engine.paper_broker import PaperBroker
from engine.paper_scalper import PaperScalperBacktester
from engine.risk_engine import TradePlan
from engine.trading_config import LIVE_EXECUTION, PAPER_ONLY, StrategyConfig


def _synthetic_frame(periods: int = 4200) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=periods, freq="min")
    x = np.arange(periods)
    close = 1.10 + x * 0.000002 + np.sin(x / 11.0) * 0.00035
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.00012
    low = np.minimum(open_, close) - 0.00012
    volume = 100 + (x % 37)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_stack_is_paper_only_and_backtest_runs():
    assert PAPER_ONLY is True
    assert LIVE_EXECUTION is False
    bt = PaperScalperBacktester(StrategyConfig())
    assert bt.can_execute_live() is False
    result = bt.run("EURUSD", _synthetic_frame(), monte_carlo_simulations=10)
    assert result.symbol == "EURUSD"
    assert "win_rate" in result.metrics
    assert "profit_factor" in result.metrics
    assert "max_drawdown" in result.metrics
    assert len(result.monte_carlo) == 10


def test_paper_broker_applies_conservative_stop_logic():
    cfg = StrategyConfig()
    broker = PaperBroker(cfg, 10_000)
    plan = TradePlan(symbol="EURUSD", side=1, entry=1.1000, stop=1.0990, target=1.10125, quantity_lots=0.1, initial_risk_currency=10.0, atr=0.0005)
    ts = pd.Timestamp("2026-01-01 10:00")
    broker.open_trade(plan, ts, 0.0001)
    bar = pd.Series({"high": 1.1020, "low": 1.0980, "close": 1.1000, "spread": 0.0001})
    closed = broker.update_symbol("EURUSD", ts + pd.Timedelta(minutes=1), bar)
    assert len(closed) == 1
    assert closed[0].exit_reason == "stop_same_bar"
    assert broker.can_execute_live() is False
