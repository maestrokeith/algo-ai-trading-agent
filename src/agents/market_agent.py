"""Market data collection agent."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.intelligence.schemas import MarketContext
from src.universe import MarketCalendar

log = logging.getLogger(__name__)


class MarketAgent:
    """Collect current quote, recent bars, and derived market features."""

    def __init__(self, config: dict[str, Any] | None = None, broker: Any | None = None) -> None:
        self.config = config or {}
        self.broker = broker
        self.calendar = MarketCalendar(self.config)
        self._cache: dict[str, MarketContext] = {}

    def observe(self, symbol: str, *, bars: pd.DataFrame | None = None, now: datetime | None = None) -> MarketContext:
        now = now or datetime.now(timezone.utc)
        symbol_u = symbol.strip().upper()
        quote = self._latest_quote(symbol_u)
        bars = bars if bars is not None else self._latest_bars(symbol_u)
        warnings: list[str] = []

        bid = _float_attr(quote, "bid")
        ask = _float_attr(quote, "ask")
        last = _float_attr(quote, "last")
        mid = _float_attr(quote, "mid")
        spread_pct = _float_attr(quote, "spread_pct")
        quote_ts = getattr(quote, "timestamp", None)
        quote_age = None
        if quote_ts is not None:
            qts = quote_ts if quote_ts.tzinfo else quote_ts.replace(tzinfo=timezone.utc)
            quote_age = max(0.0, (now - qts.astimezone(timezone.utc)).total_seconds())
        if bid is not None and ask is not None and ask < bid:
            warnings.append("crossed_quote")
        if getattr(quote, "skip_spread_check", False):
            warnings.append("incomplete_nbbo")

        returns: list[float] = []
        volume = None
        vwap = None
        volatility = None
        recent_rows: list[dict[str, Any]] = []
        if bars is not None and not getattr(bars, "empty", True):
            df = bars.tail(30).copy()
            close = df["close"].astype(float) if "close" in df else pd.Series(dtype=float)
            if len(close) >= 2:
                returns = [float(x) for x in close.pct_change().dropna().tail(10)]
                volatility = float(pd.Series(returns).std()) if returns else None
            if "volume" in df:
                volume = float(df["volume"].iloc[-1])
            if {"close", "volume"}.issubset(df.columns) and float(df["volume"].sum() or 0) > 0:
                vwap = float((df["close"] * df["volume"]).sum() / df["volume"].sum())
            recent_rows = df.tail(5).reset_index(drop=True).to_dict("records")

        last_price = first_positive(last, mid, bid, ask, _last_close(bars))
        distance_from_vwap = None
        if vwap and last_price:
            distance_from_vwap = (last_price - vwap) / vwap * 100.0
        rel_volume = _relative_volume(bars)
        session = self.calendar.get_session_at(now).value
        ctx = MarketContext(
            symbol=symbol_u,
            timestamp=now,
            last_price=float(last_price or 0.0),
            bid=bid,
            ask=ask,
            spread_pct=spread_pct,
            volume=volume,
            relative_volume=rel_volume,
            vwap=vwap,
            distance_from_vwap_pct=distance_from_vwap,
            market_session=session,
            recent_returns=tuple(returns),
            volatility=volatility,
            recent_bars=tuple(recent_rows),
            quote_age_seconds=quote_age,
            warnings=tuple(warnings),
        )
        self._cache[symbol_u] = ctx
        return ctx

    def _latest_quote(self, symbol: str) -> Any | None:
        if self.broker is None:
            return None
        if hasattr(self.broker, "get_latest_quote"):
            return self.broker.get_latest_quote(symbol)
        if hasattr(self.broker, "get_quote"):
            return self.broker.get_quote(symbol)
        return None

    def _latest_bars(self, symbol: str) -> pd.DataFrame | None:
        if self.broker is None or not hasattr(self.broker, "get_bars"):
            return None
        try:
            return self.broker.get_bars(symbol, limit=60)
        except TypeError:
            try:
                return self.broker.get_bars(symbol)
            except Exception:
                log.debug("Could not load bars for %s", symbol, exc_info=True)
                return None
        except Exception:
            log.debug("Could not load bars for %s", symbol, exc_info=True)
            return None


def _float_attr(obj: Any, attr: str) -> float | None:
    if obj is None:
        return None
    try:
        value = getattr(obj, attr)
    except Exception:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def first_positive(*values: float | None) -> float | None:
    for value in values:
        if value is not None and value > 0:
            return float(value)
    return None


def _last_close(bars: pd.DataFrame | None) -> float | None:
    if bars is None or getattr(bars, "empty", True) or "close" not in bars:
        return None
    return float(bars["close"].iloc[-1])


def _relative_volume(bars: pd.DataFrame | None) -> float | None:
    if bars is None or getattr(bars, "empty", True) or "volume" not in bars or len(bars) < 6:
        return None
    recent = float(bars["volume"].iloc[-1])
    avg = float(bars["volume"].iloc[:-1].tail(20).mean())
    if avg <= 0:
        return None
    return recent / avg
