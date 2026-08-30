"""Breakout candidate detection and ranking helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

import pandas as pd

from src.strategy_v2.entry_signals import rsi_wilder_last


SignalSnapshot = Mapping[str, float | str]

DEFAULT_SYMBOL_SECTORS: dict[str, str] = {
    "SPY": "finance",
    "QQQ": "tech",
    "IWM": "industrial",
    "DIA": "industrial",
    "VOO": "finance",
    "XLK": "tech",
    "XLF": "finance",
    "XLE": "energy",
    "XLV": "health",
    "XLI": "industrial",
    "XLY": "finance",
    "XLP": "health",
    "XLB": "industrial",
    "XLU": "industrial",
    "SMH": "tech",
    "AAPL": "tech",
    "MSFT": "tech",
    "NVDA": "tech",
    "AMZN": "tech",
    "META": "tech",
    "GOOGL": "tech",
    "AVGO": "tech",
    "AMD": "tech",
    "ORCL": "tech",
    "NFLX": "tech",
    "PLTR": "tech",
    "CRWD": "tech",
    "DDOG": "tech",
    "NOW": "tech",
    "SNOW": "tech",
    "UBER": "industrial",
    "SHOP": "tech",
    "SQ": "tech",
    "ARM": "tech",
    "MU": "tech",
    "JPM": "finance",
    "BAC": "finance",
    "GS": "finance",
    "MS": "finance",
    "WFC": "finance",
    "CAT": "industrial",
    "GE": "industrial",
    "DE": "industrial",
    "ETN": "industrial",
    "PH": "industrial",
    "WMT": "health",
    "COST": "health",
    "PG": "health",
    "KO": "health",
    "PEP": "health",
    "LLY": "health",
    "JNJ": "health",
    "ABBV": "health",
    "UNH": "health",
    "ISRG": "health",
}


def breakout_signal(snapshot: SignalSnapshot) -> bool:
    """Return ``True`` when the intraday breakout gate stack passes."""

    return (
        float(snapshot["volume_5m"]) > 2.5 * float(snapshot["avg_volume_5m"])
        and float(snapshot["price"]) > float(snapshot["vwap"])
        and float(snapshot["price"]) > float(snapshot["morning_high"])
        and float(snapshot["ema9"]) > float(snapshot["ema21"])
        and 55 < float(snapshot["rsi_5m"]) < 75
    )


def not_extended(snapshot: SignalSnapshot) -> bool:
    """Reject breakouts that are too stretched above the fast EMA."""

    ema9 = float(snapshot["ema9"])
    if ema9 <= 0:
        return False
    return (float(snapshot["price"]) - ema9) / ema9 < 0.015


def breakout_score(snapshot: SignalSnapshot) -> float:
    """Score a breakout candidate for descending rank order."""

    vwap = max(float(snapshot["vwap"]), 1e-9)
    return (
        0.4 * float(snapshot["volume_spike_ratio"])
        + 0.3 * ((float(snapshot["price"]) - vwap) / vwap)
        + 0.2 * float(snapshot["trend_strength"])
        - 0.1 * float(snapshot["spread_pct"])
    )


def infer_symbol_sector(symbol: str) -> str | None:
    """Best-effort sector key for a traded symbol."""

    return DEFAULT_SYMBOL_SECTORS.get(str(symbol).upper())


def build_breakout_snapshot(
    *,
    symbol: str,
    sector: str,
    bars_1m: pd.DataFrame,
    spread_pct: float,
) -> dict[str, float | str] | None:
    """Build breakout inputs from 1-minute intraday bars."""

    if bars_1m.empty or len(bars_1m) < 30:
        return None

    if not isinstance(bars_1m.index, pd.DatetimeIndex):
        return None

    bars = bars_1m.copy()
    for col in ("open", "high", "low", "close", "volume"):
        bars[col] = bars[col].astype(float)

    bars_5m = bars.resample("5min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    if bars_5m.empty or len(bars_5m) < 21:
        return None

    close_5m = bars_5m["close"]
    ema9 = float(close_5m.ewm(span=9, adjust=False).mean().iloc[-1])
    ema21 = float(close_5m.ewm(span=21, adjust=False).mean().iloc[-1])
    rsi_5m = rsi_wilder_last(close_5m, period=14)
    if rsi_5m is None:
        return None

    volume_5m = float(bars_5m["volume"].iloc[-1])
    avg_volume_5m = float(bars_5m["volume"].iloc[:-1].tail(12).mean()) if len(bars_5m) > 1 else 0.0
    if avg_volume_5m <= 0:
        return None

    volume = bars["volume"]
    volume_total = float(volume.sum())
    if volume_total <= 0:
        return None
    typical_price = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    vwap = float((typical_price * volume).sum() / volume_total)

    index_et = bars.index.tz_convert("America/New_York") if bars.index.tz is not None else bars.index
    morning_cutoff = index_et[0].normalize() + pd.Timedelta(hours=10, minutes=30)
    morning_mask = index_et <= morning_cutoff
    morning_slice = bars.loc[morning_mask]
    morning_high = float(morning_slice["high"].max()) if not morning_slice.empty else float(bars["high"].cummax().iloc[-1])

    price = float(bars["close"].iloc[-1])
    trend_strength = 0.0 if ema21 <= 0 else (ema9 - ema21) / ema21

    return {
        "symbol": str(symbol).upper(),
        "sector": sector,
        "volume_5m": volume_5m,
        "avg_volume_5m": avg_volume_5m,
        "price": price,
        "vwap": vwap,
        "morning_high": morning_high,
        "ema9": ema9,
        "ema21": ema21,
        "rsi_5m": float(rsi_5m),
        "volume_spike_ratio": volume_5m / avg_volume_5m,
        "trend_strength": trend_strength,
        "spread_pct": float(spread_pct),
    }


def find_breakouts(
    universe: Iterable[SignalSnapshot],
    top_sectors: Iterable[str],
) -> list[SignalSnapshot]:
    """Return the top two ranked breakout candidates from the leading sectors."""

    top_sector_set = set(top_sectors)
    candidates: list[SignalSnapshot] = []

    for snapshot in universe:
        if snapshot["sector"] not in top_sector_set:
            continue
        if breakout_signal(snapshot) and not_extended(snapshot):
            candidates.append(snapshot)

    ranked = sorted(candidates, key=breakout_score, reverse=True)
    return ranked[:2]
