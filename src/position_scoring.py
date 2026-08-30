"""Heuristic score (0–100) for an open position vs optional bar-level market fields.

**Strength score** (unweighted, open longs, ``[0, 3]``):

| strength_score = momentum_score + pnl_leg + trend_strength |

Specs sometimes write the middle term *pnl_pct* for brevity. In this codebase *pnl_leg* is
always the ``[0, 1]`` P/L subscore from :func:`pnl_score_01` (unrealized return encoded from
**fractional** P/L, e.g. ``0.03`` = +3%), **not** raw *percent* points. Use
:func:`strength_score` and :func:`composite_position_score` to compute the sum. Each of the three
terms is clamped to ``[0, 1]``.

Weighted blend **0–1** (40% / 40% / 20%): :func:`position_score` (``pos`` object or mapping),
:func:`position_score_weighted`, and :func:`weighted_composite_position_score` — use
:func:`pnl_score_01` for the PnL leg so it matches
momentum and trend on ``[0, 1]`` (raw fractional P/L without mapping is not comparable).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Sequence

import pandas as pd

from .signal_ranking import trend_momentum_volume_subscores

# Min :func:`score_position` for live-loop post-fill cooldown bypass (see ``run_alpaca_loop``).
COOLDOWN_BYPASS_MIN_SIGNAL_SCORE = 85

# :func:`position_score_weighted` — PnL / momentum / trend weights (each input on ``[0, 1]``).
POSITION_SCORE_WEIGHT_PNL = 0.4
POSITION_SCORE_WEIGHT_MOMENTUM = 0.4
POSITION_SCORE_WEIGHT_TREND = 0.2


def _clamp01_score(x: float) -> float:
    if x != x:  # NaN
        return 0.0
    return max(0.0, min(1.0, float(x)))


def pnl_score_01(pos_dict: Mapping[str, Any]) -> float:
    """
    Map Alpaca-style fractional unrealized P/L (e.g. ``0.03`` = +3%%) to ``[0, 1]``.

    Flat P/L → ~0.5; roughly ±20%% spans toward 0 / 1.
    """
    raw = None
    for k in ("unrealized_plpc", "unrealized_intraday_plpc"):
        if k in pos_dict and pos_dict[k] is not None:
            raw = pos_dict[k]
            break
    if raw is None:
        return 0.5
    try:
        plpc = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return _clamp01_score(0.5 + plpc * 2.5)


def position_score_weighted(
    unrealized_pl_pct: float,
    momentum_score: float,
    trend_score: float,
) -> float:
    """
    Weighted position quality on ``[0, 1]`` when all three inputs are on ``[0, 1]``::

        0.4 * unrealized_pl_pct + 0.4 * momentum_score + 0.2 * trend_score

    *unrealized_pl_pct* should be the normalized PnL score from :func:`pnl_score_01` (not raw
    fractional return), so it is comparable to *momentum_score* and *trend_score* from bar logic.
    """
    p = _clamp01_score(unrealized_pl_pct)
    m = _clamp01_score(momentum_score)
    t = _clamp01_score(trend_score)
    return (
        POSITION_SCORE_WEIGHT_PNL * p
        + POSITION_SCORE_WEIGHT_MOMENTUM * m
        + POSITION_SCORE_WEIGHT_TREND * t
    )


def _pos_score_field(pos: Any, key: str, *, default: float = 0.5) -> float:
    if isinstance(pos, Mapping):
        raw = pos.get(key, default)
    else:
        raw = getattr(pos, key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def position_score(pos: Any) -> float:
    """
    Weighted blend from *pos* — same formula as :func:`position_score_weighted`::

        pos.unrealized_pl_pct * 0.4 + pos.momentum_score * 0.4 + pos.trend_score * 0.2

    *pos* may be any object with those attributes, or a :class:`~collections.abc.Mapping` with those
    keys. Values are clamped to ``[0, 1]`` inside :func:`position_score_weighted`; use
    :func:`pnl_score_01` for the PnL leg when building *pos*. Missing fields default to ``0.5``.
    """
    return position_score_weighted(
        _pos_score_field(pos, "unrealized_pl_pct"),
        _pos_score_field(pos, "momentum_score"),
        _pos_score_field(pos, "trend_score"),
    )


@dataclass(frozen=True)
class PositionScoreComponents:
    """Container matching a typical ``pos`` split (map broker P/L through :func:`pnl_score_01`)."""

    unrealized_pl_pct: float
    momentum_score: float
    trend_score: float

    def combined(self) -> float:
        """Same as :func:`position_score` / :func:`position_score_weighted` on the three fields."""
        return position_score(self)


def strength_score(
    momentum_score: float,
    pnl_score: float,
    trend_strength: float,
) -> float:
    """
    **Strength score** — unweighted sum of three comparable subscores in ``[0, 1]``:

    ``strength_score = momentum_score + pnl_leg + trend_strength``  →  ``[0, 3]``.

    * *pnl_leg* is the P/L subscore (sometimes labeled *pnl* / *pnl_pct* in specs) from
      :func:`pnl_score_01`, not the raw *percent* scale from :func:`_pnl_pct` (e.g. ``+3.0`` for
      +3%%) unless you re-map it.
    * *momentum_score* and *trend_strength* are typically from
      :func:`src.signal_ranking.trend_momentum_volume_subscores` (each in ``[0, 1]``).

    Each input is clamped to ``[0, 1]`` before summing. Same result as
    :func:`composite_position_score` (without bars) when both momentum and trend are neutral
    ``0.5`` and *pnl_score* matches the position.
    """
    m = _clamp01_score(momentum_score)
    p = _clamp01_score(pnl_score)
    t = _clamp01_score(trend_strength)
    return m + p + t


def composite_position_score(
    symbol_upper: str,
    positions: Sequence[Mapping[str, Any]] | None,
    df: pd.DataFrame | None,
    *,
    ma_slow: int = 200,
    momentum_bars: int = 10,
    volume_bars: int = 20,
) -> tuple[float, dict[str, float]]:
    """
    Three-term composite for an open long (same bar windows as signal composite rank):

    **score** = **strength score** = :func:`strength_score` = ``momentum_score`` + ``pnl_leg`` +
    ``trend_strength`` (``pnl_leg`` = :func:`pnl_score_01`; often written *pnl* / *pnl_pct* in
    specs, still on ``[0, 1]``).

    Each term in ``[0, 1]``, so **score** ∈ ``[0, 3]``.

    * **pnl_leg** (**pnl_score** in the breakdown) — from broker unrealized P/L via :func:`pnl_score_01`.
    * **momentum_score** / **trend_strength** — from :func:`src.signal_ranking.trend_momentum_volume_subscores`.

    When daily *df* is missing or unusable, **momentum** and **trend_strength** default to ``0.5`` (neutral).
    """
    su = str(symbol_upper).strip().upper()
    pdct = position_dict_for_signal_score(su, positions)
    pnl_score = pnl_score_01(pdct)
    momentum_score = 0.5
    trend_strength = 0.5
    if df is not None and isinstance(df, pd.DataFrame) and not df.empty and "close" in df.columns:
        tvm = trend_momentum_volume_subscores(
            df,
            ma_slow=int(ma_slow),
            momentum_bars=int(momentum_bars),
            volume_bars=int(volume_bars),
        )
        momentum_score = float(tvm["momentum"])
        trend_strength = float(tvm["trend_strength"])
    total = strength_score(momentum_score, pnl_score, trend_strength)
    breakdown = {
        "pnl_score": float(pnl_score),
        "momentum_score": float(momentum_score),
        "trend_strength": float(trend_strength),
        "score": float(total),
    }
    return float(total), breakdown


def weakest_position_score_momentum_pnl_trend(
    symbol_upper: str,
    positions: Sequence[Mapping[str, Any]] | None,
    df: pd.DataFrame | None,
    *,
    ma_slow: int = 200,
    momentum_bars: int = 10,
    volume_bars: int = 20,
) -> float:
    """
    **Weakest (simple) —** one number per line to rank open longs: **lowest score = weakest**.

    ``score = momentum_score + pnl_leg + trend_strength``  (each in ``[0, 1]``) → **``[0, 3]``**.

    * *pnl_leg* is often written *pnl_pct* in specs; it is :func:`pnl_score_01` (unrealized P/L in
      comparable ``[0, 1]`` units), not raw *percent* points.
    * *momentum_score* and *trend_strength* come from :func:`trend_momentum_volume_subscores` when
      *df* is valid; else neutral ``0.5`` each.

    Same value as the total from :func:`composite_position_score`. Use ``min(symbols, key=...)`` to
    pick the weakest (or ``weakest_pick: composite_position_score`` / ``weakest_simple`` in
    ``portfolio.replacement``).
    """
    t, _bd = composite_position_score(
        symbol_upper,
        positions,
        df,
        ma_slow=ma_slow,
        momentum_bars=momentum_bars,
        volume_bars=volume_bars,
    )
    return float(t)


def weighted_composite_position_score(
    symbol_upper: str,
    positions: Sequence[Mapping[str, Any]] | None,
    df: pd.DataFrame | None,
    *,
    ma_slow: int = 200,
    momentum_bars: int = 10,
    volume_bars: int = 20,
) -> tuple[float, dict[str, float]]:
    """
    Same inputs as :func:`composite_position_score`, but score is the weighted blend on ``[0, 1]``::

        0.4 * pnl_score + 0.4 * momentum + 0.2 * trend_strength
    """
    su = str(symbol_upper).strip().upper()
    pdct = position_dict_for_signal_score(su, positions)
    pnl_score = pnl_score_01(pdct)
    momentum_score = 0.5
    trend_strength = 0.5
    if df is not None and isinstance(df, pd.DataFrame) and not df.empty and "close" in df.columns:
        tvm = trend_momentum_volume_subscores(
            df,
            ma_slow=int(ma_slow),
            momentum_bars=int(momentum_bars),
            volume_bars=int(volume_bars),
        )
        momentum_score = float(tvm["momentum"])
        trend_strength = float(tvm["trend_strength"])
    w = position_score_weighted(pnl_score, momentum_score, trend_strength)
    breakdown = {
        "pnl_score": float(pnl_score),
        "momentum_score": float(momentum_score),
        "trend_strength": float(trend_strength),
        "weighted_score": float(w),
    }
    return float(w), breakdown


def _to_float(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _symbol_key(p: Mapping[str, Any]) -> str:
    return str(p.get("symbol") or "").strip().upper()


def _market_row_for_symbol(
    market_data: Mapping[str, Any],
    symbol: str,
) -> Mapping[str, Any] | None:
    if not symbol:
        return None
    if symbol in market_data:
        row = market_data[symbol]
        return row if isinstance(row, Mapping) else None
    for k, v in market_data.items():
        if str(k).strip().upper() == symbol:
            return v if isinstance(v, Mapping) else None
    return None


def _pnl_pct(p: Mapping[str, Any]) -> float:
    """Unrealized P/L as percent of cost (e.g. 3.0 == +3%%)."""
    raw = p.get("unrealized_plpc")
    if raw is not None:
        v = _to_float(raw)
        if v is not None:
            return v * 100.0
    alt = p.get("unrealized_intraday_plpc")
    if alt is not None:
        v = _to_float(alt)
        if v is not None:
            return v * 100.0
    return 0.0


def score_position(
    p: Mapping[str, Any],
    market_data: Mapping[str, Any],
) -> int:
    """
    Score an existing long-style position on a 0–100 scale.

    ``p`` may include ``symbol``, ``unrealized_plpc`` (fractional, e.g. 0.03 for +3%),
    ``bars_held``, etc. ``market_data`` maps symbol (any case) to a row with numeric
    ``close``, ``ma_fast``, and ``ma_slow``. Missing trend fields skip trend/momentum
    adjustments; PnL and holding-time rules still apply.
    """
    score = 50

    pnl_pct = _pnl_pct(p)

    if pnl_pct > 3:
        score += 20
    elif pnl_pct > 1:
        score += 10
    elif pnl_pct < -2:
        score -= 20
    elif pnl_pct < -1:
        score -= 10

    sym = _symbol_key(p)
    row = _market_row_for_symbol(market_data, sym) if sym else None
    if row:
        price = _to_float(row.get("close"))
        ma_fast = _to_float(row.get("ma_fast"))
        ma_slow = _to_float(row.get("ma_slow"))
        if price is not None and ma_fast is not None and ma_slow is not None:
            if price > ma_fast > ma_slow:
                score += 15
            elif price < ma_fast:
                score -= 15

            if price < ma_fast:
                score -= 10

    bars_held_raw = p.get("bars_held", 0)
    bars_held = int(_to_float(bars_held_raw) or 0)

    if bars_held > 20:
        score -= 10
    if bars_held > 40:
        score -= 20

    return max(0, min(100, score))


def position_dict_for_signal_score(
    symbol_upper: str,
    positions: Sequence[Mapping[str, Any]] | Sequence[Any] | None,
) -> dict[str, Any]:
    """
    Build the ``p`` mapping for :func:`score_position` from Alpaca-style ``get_positions()`` rows.

    Uses ``unrealized_plpc`` / ``unrealized_intraday_plpc`` when present, else ``unrealized_pl`` /
    ``cost_basis`` to derive a fractional P/L for scoring.
    """
    su = str(symbol_upper or "").strip().upper()
    base: dict[str, Any] = {"symbol": su}
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        if str(p.get("symbol") or "").strip().upper() != su:
            continue
        _set = False
        for key in ("unrealized_intraday_plpc", "unrealized_plpc"):
            raw = p.get(key)
            if raw is not None and str(raw).strip() != "":
                v = _to_float(raw)
                if v is not None:
                    base["unrealized_plpc"] = v
                    _set = True
                    break
        if not _set:
            ur = _to_float(p.get("unrealized_pl"))
            cb = _to_float(p.get("cost_basis"))
            if ur is not None and cb is not None and cb > 0:
                base["unrealized_plpc"] = ur / cb
        break
    return base
