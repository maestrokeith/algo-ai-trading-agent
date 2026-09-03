"""Market-data normalization and lookahead-safe resampling helpers."""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from .trading_config import InstrumentSpec

_REQUIRED = ("open", "high", "low", "close", "volume")


def normalize_ohlcv(frame: pd.DataFrame, spec: InstrumentSpec) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("market data is empty")
    missing = [c for c in _REQUIRED if c not in frame.columns]
    if missing:
        raise ValueError(f"missing OHLCV columns: {missing}")
    out = frame.copy()
    out.index = pd.to_datetime(out.index)
    out = out[~out.index.duplicated(keep="last")].sort_index()
    for col in _REQUIRED:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    if "spread" not in out:
        out["spread"] = spec.default_spread
    else:
        out["spread"] = pd.to_numeric(out["spread"], errors="coerce").fillna(spec.default_spread)
    out = out.dropna(subset=list(_REQUIRED))
    bad = (out["high"] < out[["open", "close"]].max(axis=1)) | (out["low"] > out[["open", "close"]].min(axis=1))
    if bad.any():
        raise ValueError("invalid OHLC relationships detected")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("prices must be positive")
    if (out["volume"] < 0).any():
        raise ValueError("volume cannot be negative")
    return out


def resample_ohlcv(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "spread": "last"}
    out = frame.resample(rule, label="right", closed="right").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


def align_completed_htf(htf: pd.DataFrame, ltf_index: pd.DatetimeIndex, columns: Iterable[str]) -> pd.DataFrame:
    selected = htf.loc[:, list(columns)].shift(1)
    return selected.reindex(ltf_index, method="ffill")


def session_name(timestamp: pd.Timestamp) -> str:
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("UTC")
    hour = ts.hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 13:
        return "london"
    if 13 <= hour < 17:
        return "overlap"
    if 17 <= hour < 22:
        return "new_york"
    return "off_hours"
