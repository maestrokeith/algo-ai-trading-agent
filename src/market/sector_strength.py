"""Sector momentum ranking helpers."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd


SECTOR_ETFS = {
    "tech": "XLK",
    "finance": "XLF",
    "energy": "XLE",
    "health": "XLV",
    "industrial": "XLI",
}


SectorSnapshot = Mapping[str, float]


def sector_strength(data: SectorSnapshot) -> float:
    """Return a simple composite intraday strength score for a sector."""

    return (
        (float(data["price"]) - float(data["vwap"])) / float(data["vwap"])
        + float(data["momentum_30m"])
        + float(data["relative_volume"])
    )


def build_sector_snapshot(bars_1m: pd.DataFrame) -> dict[str, float] | None:
    """Build sector-strength inputs from 1-minute intraday bars."""

    if bars_1m.empty or len(bars_1m) < 30:
        return None

    close = bars_1m["close"].astype(float)
    volume = bars_1m["volume"].astype(float)
    price = float(close.iloc[-1])
    vol_sum = float(volume.sum())
    if vol_sum <= 0:
        return None

    typical_price = (bars_1m["high"] + bars_1m["low"] + bars_1m["close"]) / 3.0
    vwap = float((typical_price * volume).sum() / vol_sum)
    base_close = float(close.iloc[-30])
    momentum_30m = 0.0 if base_close <= 0 else (price - base_close) / base_close

    recent_volume = float(volume.iloc[-30:].sum())
    if len(volume) >= 60:
        baseline_volume = float(volume.iloc[:-30].tail(30).sum())
        relative_volume = recent_volume / baseline_volume if baseline_volume > 0 else 0.0
    else:
        avg_1m = float(volume.mean())
        relative_volume = recent_volume / max(avg_1m * 30.0, 1e-9)

    return {
        "price": price,
        "vwap": vwap,
        "momentum_30m": momentum_30m,
        "relative_volume": relative_volume,
    }


def get_top_sectors(sector_data: Mapping[str, SectorSnapshot]) -> list[str]:
    """Rank sectors by strength and return the top two names."""

    ranked = sorted(
        sector_data.items(),
        key=lambda item: sector_strength(item[1]),
        reverse=True,
    )
    return [sector_name for sector_name, _ in ranked[:2]]
