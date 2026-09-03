import pytest

from engine.risk_engine import RiskEngine
from engine.trading_config import StrategyConfig


def test_risk_fraction_capped_at_one_percent():
    with pytest.raises(ValueError):
        StrategyConfig(risk_fraction=0.02)


def test_position_size_respects_risk_budget():
    cfg = StrategyConfig(initial_equity=10_000, risk_fraction=0.005)
    plan = RiskEngine(cfg).plan(symbol="EURUSD", side=1, entry=1.1000, atr_value=0.0005, recent_low=1.0995, recent_high=1.1005, equity=10_000)
    assert plan.quantity_lots >= 0.01
    assert plan.initial_risk_currency <= 50.0 + 1e-6
    assert plan.stop < plan.entry < plan.target
