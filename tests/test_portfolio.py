from engine.portfolio import PortfolioRiskBook
from engine.risk_engine import TradePlan
from engine.trading_config import StrategyConfig


def _plan(symbol: str, risk: float = 50.0) -> TradePlan:
    return TradePlan(symbol=symbol, side=1, entry=1.0, stop=0.99, target=1.0125, quantity_lots=0.1, initial_risk_currency=risk, atr=0.005)


def test_portfolio_enforces_symbol_limit():
    cfg = StrategyConfig(max_positions=3, max_positions_per_symbol=1)
    book = PortfolioRiskBook(cfg)
    first = _plan("EURUSD")
    book.register("a", first, 10_000)
    ok, reason = book.can_open(_plan("EURUSD"), 10_000)
    assert not ok
    assert reason == "symbol_limit"


def test_portfolio_enforces_total_risk_limit():
    cfg = StrategyConfig(max_total_open_risk_fraction=0.01, max_positions=5)
    book = PortfolioRiskBook(cfg)
    book.register("a", _plan("EURUSD", 60), 10_000)
    ok, reason = book.can_open(_plan("GBPUSD", 50), 10_000)
    assert not ok
    assert reason == "portfolio_risk_limit"
