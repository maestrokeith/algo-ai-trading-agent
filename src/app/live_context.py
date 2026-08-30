"""Shared live-loop context helpers extracted from the legacy script entrypoint."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytz

from src.brokers.alpaca_client import QuoteInfo
from src.brokers.alpaca_client import AlpacaBroker

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def allocator_skip_due_cooldown(_user_id: str) -> bool:
    """Post-bulk-trim 1-cycle allocator skip (per user). Currently always off."""
    return False


def quote_skip_spread_check(q: QuoteInfo | None) -> bool:
    """No NBBO or one-sided quote — do not compare spread_pct to caps or execution spread gate."""
    return q is None or bool(getattr(q, "skip_spread_check", False))


def normalize_broker_positions(positions: list[Any] | None) -> list[dict[str, Any]]:
    """Alpaca dict rows or position objects → rows for :func:`src.exposure.compute_exposures`."""
    normalized_positions: list[dict[str, Any]] = []
    for p in positions or []:
        if isinstance(p, dict):
            sym = p.get("symbol")
            market_value = float(p.get("market_value") or 0.0)
            side = str(p.get("side", "long"))
            _ur = p.get("unrealized_pl")
        else:
            sym = getattr(p, "symbol", None)
            market_value = float(getattr(p, "market_value", 0.0))
            side = str(getattr(p, "side", "long"))
            _ur = getattr(p, "unrealized_pl", None)
        try:
            pnl_row = float(_ur) if _ur is not None and str(_ur).strip() != "" else 0.0
        except (TypeError, ValueError):
            pnl_row = 0.0
        su = str(sym or "").strip().upper()
        if not su:
            continue
        row: dict[str, Any] = {
            "symbol": su,
            "market_value": abs(market_value),
            "side": str(side).lower(),
            "pnl": pnl_row,
        }
        if isinstance(p, dict):
            for k in (
                "qty",
                "options_delta_notional",
                "options_delta_adjusted",
                "delta",
                "underlying_last",
                "underlying_price",
            ):
                v = p.get(k)
                if v is not None and str(v).strip() != "":
                    row[k] = v
        else:
            for k in (
                "qty",
                "options_delta_notional",
                "options_delta_adjusted",
                "delta",
                "underlying_last",
                "underlying_price",
            ):
                v = getattr(p, k, None)
                if v is not None and str(v).strip() != "":
                    row[k] = v
        normalized_positions.append(row)
    return normalized_positions


def calc_small_position_size(equity: float) -> float:
    """Capital budget for one breakout trade."""
    return float(equity) * 0.02


def breakout_module_cfg(config: dict[str, Any]) -> dict[str, Any]:
    """Normalized breakout module config."""
    raw = config.get("breakout_module")
    return raw if isinstance(raw, dict) else {}


def max_breakout_exposure(config: dict[str, Any], equity: float) -> float:
    """Maximum aggregate capital allocated to breakout trades."""
    breakout_cfg = breakout_module_cfg(config)
    pct = float(breakout_cfg.get("max_total_exposure_pct", 6.0) or 6.0)
    return float(equity) * (pct / 100.0)


def session_vwap_and_ema9(
    broker: AlpacaBroker,
    symbol: str,
    dt: datetime,
) -> tuple[float | None, float | None]:
    """Best-effort intraday session VWAP and 9-period EMA from 1-minute bars."""
    start_et = dt.astimezone(pytz.timezone("America/New_York")).replace(
        hour=9, minute=30, second=0, microsecond=0
    )
    bars = broker.get_bars(
        symbol,
        timeframe="1Min",
        start=start_et.astimezone(pytz.UTC),
        end=dt.astimezone(pytz.UTC),
        limit=390,
    )
    if bars.empty:
        return None, None

    volume = bars["volume"].astype(float)
    if float(volume.sum()) <= 0:
        vwap = None
    else:
        typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3.0
        vwap = float((typical_price * volume).sum() / volume.sum())

    ema9 = None
    close = bars["close"].astype(float)
    if not close.empty:
        ema9 = float(close.ewm(span=9, adjust=False).mean().iloc[-1])
    return vwap, ema9

