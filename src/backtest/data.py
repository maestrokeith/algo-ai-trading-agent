"""
Load OHLCV data for backtesting: from CSV directory or Alpaca API.

CSV format: files named SYMBOL.csv with columns: date, open, high, low, close, volume.
Date column can be parseable datetime (e.g. YYYY-MM-DD). Index is set to datetime.
"""
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any

import pandas as pd


def load_csv_data(
    data_dir: str | Path,
    symbols: list[str],
) -> dict[str, pd.DataFrame]:
    """
    Load OHLCV from CSV files. Each file data_dir/SYMBOL.csv must have:
    date, open, high, low, close, volume.
    Returns dict[symbol -> DataFrame] with datetime index, sorted by index.
    """
    data_dir = Path(data_dir)
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        path = data_dir / f"{sym}.csv"
        if not path.exists():
            continue
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if "open" not in df.columns and "Open" in df.columns:
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns and col.capitalize() in df.columns:
                df = df.rename(columns={col.capitalize(): col})
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df.sort_index()
        out[sym] = df
    return out


def load_alpaca_data(
    symbols: list[str],
    start: datetime,
    end: datetime,
    config: dict[str, Any] | None = None,
) -> dict[str, pd.DataFrame]:
    """
    Fetch OHLCV from Alpaca for the given date range.
    Requires alpaca-py and credentials. Returns dict[symbol -> DataFrame].
    """
    try:
        from ..brokers.alpaca_client import AlpacaBroker
    except ImportError:
        raise RuntimeError("Alpaca data requires algo.brokers.alpaca_client (alpaca-py)")
    broker = AlpacaBroker(config or {})
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = broker.get_bars(sym, "1Day", start=start, end=end, limit=2000)
        if df.empty or len(df) < 50:
            continue
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        out[sym] = df
    return out
