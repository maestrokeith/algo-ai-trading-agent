"""Collect broker data for the automated end-of-day trading report."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from src.exposure import SYMBOL_SECTOR, compute_exposures


@dataclass(frozen=True)
class DailyTradingReportData:
    """Inputs consumed by the daily HTML report generator."""

    account: dict[str, Any]
    positions: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    exposure: dict[str, Any]
    portfolio_history: Mapping[str, Any] | None = None


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _account_snapshot(broker: Any) -> dict[str, Any]:
    if callable(getattr(broker, "get_account_snapshot", None)):
        snapshot = broker.get_account_snapshot()
        if isinstance(snapshot, Mapping):
            return dict(snapshot)
    return {"equity": broker.get_equity() if callable(getattr(broker, "get_equity", None)) else 0.0}


def _daily_pnl(broker: Any, snapshot: Mapping[str, Any], trade_date: date) -> float:
    equity = _float_value(snapshot.get("equity"))
    last_equity = snapshot.get("last_equity")
    if last_equity is not None:
        prior = _float_value(last_equity)
        if prior > 0:
            return equity - prior
    if callable(getattr(broker, "get_portfolio_daily_pnl_for_date", None)):
        daily = broker.get_portfolio_daily_pnl_for_date(trade_date)
        if isinstance(daily, Mapping):
            return _float_value(daily.get("profit_loss"))
    return _float_value(snapshot.get("pnl_today"))


def _configured_core_symbols(config: Mapping[str, Any]) -> set[str]:
    universe = config.get("universe") if isinstance(config.get("universe"), Mapping) else {}
    symbols = universe.get("symbols") if isinstance(universe, Mapping) else []
    if isinstance(symbols, Sequence) and not isinstance(symbols, (str, bytes)):
        return {str(sym).strip().upper() for sym in symbols if str(sym).strip()}
    return set()


def _infer_strategy(order: Mapping[str, Any], core_symbols: set[str]) -> str:
    for key in ("strategy", "source", "entry_source", "signal_source"):
        raw = str(order.get(key) or "").strip()
        if raw:
            return raw
    client_id = str(order.get("client_order_id") or order.get("id") or "").lower()
    if "dynamic" in client_id:
        return "dynamic_universe"
    symbol = str(order.get("symbol") or "").strip().upper()
    if symbol and core_symbols and symbol not in core_symbols:
        return "dynamic_universe"
    return "manual_or_core"


def _order_pnl(order: Mapping[str, Any]) -> float:
    for key in ("pnl", "realized_pnl", "profit_loss", "realized_profit_loss"):
        raw = order.get(key)
        if raw is not None and str(raw).strip() != "":
            return _float_value(raw)
    return 0.0


def _order_return_pct(order: Mapping[str, Any], *, pnl: float, qty: float) -> float | None:
    for key in ("return_pct", "pnl_pct", "realized_return_pct", "profit_loss_pct"):
        raw = order.get(key)
        if raw is not None and str(raw).strip() != "":
            return _float_value(raw)
    price = _float_value(order.get("filled_avg_price"))
    notional = abs(float(qty) * price)
    if notional > 0 and pnl != 0.0:
        return (float(pnl) / notional) * 100.0
    return None


def _order_news_score(order: Mapping[str, Any]) -> float | None:
    for key in ("news_score", "dynamic_news_score", "catalyst_news_score"):
        raw = order.get(key)
        if raw is not None and str(raw).strip() != "":
            return _float_value(raw)
    return None


def _order_catalyst_type(order: Mapping[str, Any]) -> str:
    for key in ("catalyst_type", "news_catalyst_type", "event_type"):
        raw = str(order.get(key) or "").strip().lower()
        if raw:
            return raw
    return ""


def normalize_daily_trade(order: Mapping[str, Any], *, core_symbols: set[str]) -> dict[str, Any]:
    """Normalize a broker order row into the daily report trade shape."""

    qty = _float_value(order.get("qty") if order.get("qty") is not None else order.get("filled_qty"))
    pnl = _order_pnl(order)
    return {
        "id": str(order.get("id") or ""),
        "symbol": str(order.get("symbol") or "").strip().upper(),
        "side": str(order.get("side") or "").strip().lower(),
        "qty": qty,
        "filled_avg_price": order.get("filled_avg_price"),
        "filled_at": order.get("filled_at") or order.get("submitted_at"),
        "strategy": _infer_strategy(order, core_symbols),
        "pnl": pnl,
        "return_pct": _order_return_pct(order, pnl=pnl, qty=qty),
        "news_score": _order_news_score(order),
        "catalyst_type": _order_catalyst_type(order),
    }


def collect_daily_trading_report_data(
    *,
    broker: Any,
    config: Mapping[str, Any],
    trade_date: date,
) -> DailyTradingReportData:
    """Collect account, trades, positions, exposure, and history for the daily report."""

    snapshot = _account_snapshot(broker)
    equity = _float_value(snapshot.get("equity"))
    account = {"equity": equity, "pnl_today": _daily_pnl(broker, snapshot, trade_date)}
    portfolio_cfg = config.get("portfolio") if isinstance(config.get("portfolio"), Mapping) else {}
    contributed = _float_value(
        portfolio_cfg.get("total_contributed_usd", portfolio_cfg.get("total_contributed"))
        if isinstance(portfolio_cfg, Mapping)
        else None
    )
    if contributed > 0:
        account["total_contributed_usd"] = contributed

    raw_positions = broker.get_positions() if callable(getattr(broker, "get_positions", None)) else []
    positions = [dict(row) for row in (raw_positions or []) if isinstance(row, Mapping)]
    default_sector = str((config.get("sector") or {}).get("default_sector") or "unknown") if isinstance(config.get("sector"), Mapping) else "unknown"
    exposure_snapshot = compute_exposures(equity, positions, SYMBOL_SECTOR, default_sector=default_sector)
    exposure = {
        "gross": exposure_snapshot.gross_pct,
        "net": exposure_snapshot.net_pct,
        "sector": dict(exposure_snapshot.sector_pct),
    }

    raw_orders = broker.get_orders_for_date(trade_date) if callable(getattr(broker, "get_orders_for_date", None)) else []
    core_symbols = _configured_core_symbols(config)
    trades = [normalize_daily_trade(dict(order), core_symbols=core_symbols) for order in (raw_orders or []) if isinstance(order, Mapping)]
    portfolio_history = (
        broker.get_portfolio_equity_series()
        if callable(getattr(broker, "get_portfolio_equity_series", None))
        else None
    )
    return DailyTradingReportData(
        account=account,
        positions=positions,
        trades=trades,
        exposure=exposure,
        portfolio_history=portfolio_history,
    )
