"""
Optional entry gates under ``config["filters"]`` (distinct from ``trade_filters``).

* **Tape** (``min_intraday_range_pct`` / ``min_atr_pct``) for listed ``symbols``: skip entry when
  the latest bar's range% or the passed ATR% is **below** the floor (``0`` = off).
* **Trend / regime** (``require_adx``, ``require_price_above_20ema``, etc.): skip entries in
  chop / neutral tape when any enabled sub-check fails (AND across enabled flags).

On the daily ``ohlcv_df`` the live loop uses, **range%** = ``100 * (high − low) / close`` on
the last bar. **20 EMA** is ``ewm(span=20, adjust=False)`` on **close** (same as typical charts).
**ADX** is Wilder-style 14 (period configurable) from high/low/close.
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from .trade_filters import FilterResult

log = logging.getLogger(__name__)


def parse_filters_min_tape_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Return ``config["filters"]`` as a dict, or empty."""
    f = (config or {}).get("filters")
    return f if isinstance(f, dict) else {}


def listed_defensive_filter_symbols(config: dict[str, Any] | None) -> frozenset[str]:
    raw = parse_filters_min_tape_config(config).get("symbols")
    if not isinstance(raw, (list, tuple, set, frozenset)) or not raw:
        return frozenset()
    return frozenset(str(s).strip().upper() for s in raw if str(s).strip())


def bar_range_pct_last_row(ohlcv_df: Any) -> float | None:
    """
    (high − low) / close * 100 on the last bar; ``None`` if columns or values are unusable.
    """
    if ohlcv_df is None or getattr(ohlcv_df, "empty", True):
        return None
    try:
        h = ohlcv_df["high"].iloc[-1]
        l = ohlcv_df["low"].iloc[-1]
        c = ohlcv_df["close"].iloc[-1]
        hi = float(h)
        lo = float(l)
        cl = float(c)
    except (TypeError, KeyError, ValueError, IndexError) as e:
        log.debug("bar_range_pct_last_row: %s", e)
        return None
    if cl <= 0 or cl != cl or hi != hi or lo != lo:
        return None
    if hi < lo:
        return None
    return 100.0 * (hi - lo) / cl


def check_listed_defensive_tape_gates(
    config: dict[str, Any] | None,
    symbol: str,
    ohlcv_df: Any,
    atr_pct: float | None,
) -> FilterResult:
    """
    When the symbol is in ``filters.symbols`` and a minimum is ``> 0``, enforce range and/or
    ATR. Otherwise allow.
    """
    fcfg = parse_filters_min_tape_config(config)
    su = str(symbol or "").strip().upper()
    applicable = listed_defensive_filter_symbols(config)
    if not applicable or su not in applicable:
        return FilterResult(allowed=True, reason="ok")

    def _f(key: str) -> float:
        raw = fcfg.get(key)
        try:
            v = float(raw) if raw is not None and str(raw).strip() != "" else 0.0
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, v)

    min_r = _f("min_intraday_range_pct")
    min_a = _f("min_atr_pct")
    if min_r <= 0.0 and min_a <= 0.0:
        return FilterResult(allowed=True, reason="ok")

    if min_r > 0.0:
        r_pct = bar_range_pct_last_row(ohlcv_df)
        if r_pct is None:
            return FilterResult(
                allowed=False,
                reason="defensive tape filter: cannot compute range% (no OHLCV or bad bars)",
            )
        if r_pct < min_r - 1e-9:
            return FilterResult(
                allowed=False,
                reason=(
                    f"defensive tape filter: range% {r_pct:.2f} < {min_r:.2f} "
                    f"({symbol} listed under filters.symbols)"
                ),
            )
    if min_a > 0.0:
        if atr_pct is None or atr_pct != atr_pct:
            return FilterResult(
                allowed=False,
                reason="defensive tape filter: ATR% missing (cannot check min_atr_pct)",
            )
        if float(atr_pct) < min_a - 1e-9:
            return FilterResult(
                allowed=False,
                reason=(
                    f"defensive tape filter: ATR% {float(atr_pct):.2f} < {min_a:.2f} "
                    f"({symbol} listed under filters.symbols)"
                ),
            )
    return FilterResult(allowed=True, reason="ok")


def _filters_bool(fcfg: dict[str, Any], key: str, default: bool = False) -> bool:
    v = fcfg.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _wilder_rma(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / float(period), min_periods=period, adjust=False).mean()


def adx_wilder_last(ohlcv_df: Any, period: int = 14) -> float | None:
    """
    Last value of ADX(period) (Wilder). Returns ``None`` if inputs are too short or invalid.
    """
    p = int(period) if int(period) > 0 else 14
    if ohlcv_df is None or getattr(ohlcv_df, "empty", True):
        return None
    need = 2 * p + 1
    if len(ohlcv_df) < need:
        return None
    try:
        high = ohlcv_df["high"].astype(float)
        low = ohlcv_df["low"].astype(float)
        close = ohlcv_df["close"].astype(float)
    except (KeyError, TypeError, ValueError) as e:
        log.debug("adx_wilder_last: %s", e)
        return None
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=ohlcv_df.index)
    minus_dm = pd.Series(minus_dm, index=ohlcv_df.index)
    atr = _wilder_rma(tr, p)
    denom_atr = atr.replace(0.0, np.nan)
    pdi = 100.0 * _wilder_rma(plus_dm, p) / denom_atr
    mdi = 100.0 * _wilder_rma(minus_dm, p) / denom_atr
    den = (pdi + mdi).replace(0.0, np.nan)
    dx = 100.0 * (pdi - mdi).abs() / den
    adx = _wilder_rma(dx, p)
    v = adx.iloc[-1]
    if v != v:  # NaN
        return None
    return float(v)


def _ema_20_and_slope(ohlcv_df: Any, *, span: int = 20) -> tuple[float, float, float] | None:
    """(close_last, ema_last, ema_prev) or None."""
    s = int(span) if int(span) > 1 else 20
    if ohlcv_df is None or len(ohlcv_df) < s + 1:
        return None
    try:
        close = ohlcv_df["close"].astype(float)
    except (KeyError, TypeError, ValueError) as e:
        log.debug("_ema_20_and_slope: %s", e)
        return None
    ema = close.ewm(span=s, adjust=False, min_periods=s).mean()
    last_c = float(close.iloc[-1])
    last_e = float(ema.iloc[-1])
    prev_e = float(ema.iloc[-2])
    if last_e != last_e or prev_e != prev_e or last_c != last_c:
        return None
    return last_c, last_e, prev_e


def _ema_slope_tolerance_allows(
    fcfg: dict[str, Any],
    *,
    last_ema: float,
    prev_ema: float,
    momentum_score: float | None,
    breakout_continuation: bool,
) -> bool:
    raw = fcfg.get("ema_slope_tolerance")
    tol = raw if isinstance(raw, dict) else {}
    if not _filters_bool(tol, "enabled", False):
        return False
    if prev_ema <= 0:
        return False
    try:
        max_negative_pct = float(tol.get("max_negative_slope_pct", 0.02) or 0.02)
    except (TypeError, ValueError):
        max_negative_pct = 0.02
    try:
        strong_score_min = float(tol.get("strong_momentum_score_min", 80.0) or 80.0)
    except (TypeError, ValueError):
        strong_score_min = 80.0
    slope_pct = 100.0 * (last_ema - prev_ema) / prev_ema
    near_flat = slope_pct >= -abs(max_negative_pct) - 1e-12
    strong_score = momentum_score is not None and float(momentum_score) >= strong_score_min
    allow_breakout = _filters_bool(tol, "allow_breakout_continuation", True)
    return bool(near_flat and strong_score and (bool(breakout_continuation) or not allow_breakout))


def check_trend_regime_filters(
    config: dict[str, Any] | None,
    symbol: str,
    ohlcv_df: Any,
    *,
    momentum_score: float | None = None,
    breakout_continuation: bool = False,
) -> FilterResult:
    """
    When ``filters.require_adx`` and/or 20-EMA flags are on, require stronger trend / structure
    on the same ``ohlcv_df`` as entry (typically daily in live). Disabled flags = no sub-check.
    All enabled sub-checks must pass (AND).
    """
    fcfg = parse_filters_min_tape_config(config)
    require_adx = _filters_bool(fcfg, "require_adx", False)
    require_ema = _filters_bool(fcfg, "require_price_above_20ema", False)
    require_slope = _filters_bool(fcfg, "require_20ema_slope_positive", False)
    if not (require_adx or require_ema or require_slope):
        return FilterResult(allowed=True, reason="ok")

    sym_u = str(symbol or "").strip().upper()
    if ohlcv_df is None or getattr(ohlcv_df, "empty", True):
        return FilterResult(
            allowed=False,
            reason="trend filter: need OHLCV (enable filters off or pass sufficient bars)",
        )
    if require_ema or require_slope:
        m = _ema_20_and_slope(ohlcv_df, span=20)
        if m is None:
            return FilterResult(
                allowed=False,
                reason="trend filter: need >= 21 bars and valid close for 20 EMA",
            )
        last_c, last_e, prev_e = m
        if require_ema and not (last_c > last_e):
            return FilterResult(
                allowed=False,
                reason=(
                    f"trend filter: close {last_c:.4f} not above 20 EMA {last_e:.4f} ({sym_u})"
                ),
            )
        if require_slope and last_e <= prev_e + 1e-9:
            if _ema_slope_tolerance_allows(
                fcfg,
                last_ema=last_e,
                prev_ema=prev_e,
                momentum_score=momentum_score,
                breakout_continuation=breakout_continuation,
            ):
                log.info(
                    "TREND_EMA_SLOPE_TOLERATED symbol=%s ema20=%.4f prev_ema20=%.4f momentum_score=%s breakout=%s",
                    sym_u,
                    last_e,
                    prev_e,
                    "n/a" if momentum_score is None else f"{float(momentum_score):.2f}",
                    str(bool(breakout_continuation)).lower(),
                )
            else:
                return FilterResult(
                    allowed=False,
                    reason=(
                        f"trend filter: 20 EMA slope not positive ({last_e:.4f} vs {prev_e:.4f} {sym_u})"
                    ),
                )

    if require_adx:
        try:
            p = int(fcfg.get("adx_period", 14) or 14)
        except (TypeError, ValueError):
            p = 14
        p = max(2, p)
        try:
            adx_m = float(fcfg.get("adx_min", 20) or 20.0)
        except (TypeError, ValueError):
            adx_m = 20.0
        adx_m = max(0.0, adx_m)
        a = adx_wilder_last(ohlcv_df, period=p)
        if a is None:
            return FilterResult(
                allowed=False,
                reason=f"trend filter: ADX not computable (need ~{2 * p + 1}+ bars) ({sym_u})",
            )
        if a < adx_m - 1e-9:
            return FilterResult(
                allowed=False,
                reason=(f"trend filter: ADX {a:.1f} < {adx_m} ({sym_u})"),
            )
    return FilterResult(allowed=True, reason="ok")
