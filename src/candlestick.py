"""
Candlestick pattern detection from OHLC bars.

Uses the same OHLC data you already have; patterns are optional entry filters.
"""
import pandas as pd


def _body_size(row: pd.Series) -> float:
    return abs(row["close"] - row["open"])


def _upper_wick(row: pd.Series) -> float:
    return row["high"] - max(row["open"], row["close"])


def _lower_wick(row: pd.Series) -> float:
    return min(row["open"], row["close"]) - row["low"]


def _range_size(row: pd.Series) -> float:
    r = row["high"] - row["low"]
    return r if r > 0 else 1e-9


def is_bullish_candle(row: pd.Series) -> bool:
    return row["close"] > row["open"]


def is_bearish_candle(row: pd.Series) -> bool:
    return row["close"] < row["open"]


def bullish_engulfing(df: pd.DataFrame, idx: int = -1) -> bool:
    """
    Current candle is bullish and its body engulfs the previous candle's body.
    Often used as a reversal signal after a dip.
    """
    if df is None or len(df) < 2:
        return False
    curr = df.iloc[idx]
    prev = df.iloc[idx - 1]
    if not is_bullish_candle(curr) or not is_bearish_candle(prev):
        return False
    return curr["close"] >= prev["open"] and curr["open"] <= prev["close"]


def hammer(df: pd.DataFrame, idx: int = -1, lower_wick_ratio: float = 2.0) -> bool:
    """
    Small body at the top, long lower wick, little upper wick. Bullish in a pullback.
    body small vs range; lower wick >= lower_wick_ratio * body.
    """
    if df is None or len(df) < 1:
        return False
    row = df.iloc[idx]
    body = _body_size(row)
    lower = _lower_wick(row)
    upper = _upper_wick(row)
    rng = _range_size(row)
    if rng <= 0 or body <= 0:
        return False
    return (
        is_bullish_candle(row)
        and lower >= body * lower_wick_ratio
        and upper <= body * 0.5
    )


def doji_near_support(df: pd.DataFrame, idx: int = -1, body_ratio: float = 0.1) -> bool:
    """
    Very small body (open ≈ close). Optional: use with pullback context.
    body / range <= body_ratio.
    """
    if df is None or len(df) < 1:
        return False
    row = df.iloc[idx]
    body = _body_size(row)
    rng = _range_size(row)
    return body / rng <= body_ratio and rng > 0


def detect_any(df: pd.DataFrame, patterns: list[str], idx: int = -1) -> bool:
    """Return True if any of the given patterns is present on the bar at idx."""
    if not patterns or df is None or len(df) < 1:
        return True
    detectors = {
        "bullish_engulfing": bullish_engulfing,
        "hammer": hammer,
        "doji": lambda d, i: doji_near_support(d, i, 0.15),
    }
    for name in patterns:
        fn = detectors.get(name.strip().lower())
        if fn and fn(df, idx):
            return True
    return False
