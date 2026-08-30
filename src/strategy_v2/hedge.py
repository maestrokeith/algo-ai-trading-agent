"""Hedge notion in dollars from regime score (SQQQ / inverse sleeve)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.execution import ExecutionManager
from src.inverse_hedge import hedge_symbol, long_hedge_position_held


def allow_sqqq_logic(
    cfg: dict[str, Any] | None,
    positions: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any],
) -> tuple[bool, str | None]:
    """
    Inverse-sleeve gate when ``universe.require_sqqq_for_trend_long_entries`` is on in a bearish context.

    Returns ``(True, None)`` if the configured hedge symbol (e.g. SQQQ) is held long; otherwise
    ``(False, reason)``.
    """
    if long_hedge_position_held(cfg, positions, tracked):
        return True, None
    h = hedge_symbol(cfg)
    return (
        False,
        "trend longs blocked — no long %s (universe.require_sqqq_for_trend_long_entries)" % h,
    )


def trend_long_hedge_requirement_ok(
    cfg: dict[str, Any] | None,
    positions: Sequence[Mapping[str, Any]],
    tracked: Mapping[str, Any],
    *,
    regime_condition: str | None = None,
    bearish_regime: bool = False,
) -> tuple[bool, str | None]:
    """
    When ``universe.require_sqqq_for_trend_long_entries`` is false, the scan is never blocked here.

    When it is **true**, behavior depends on the market-regime **condition** (from
    ``MarketRegimeScorer``: ``bullish`` / ``neutral`` / ``defensive``), if *regime_condition*
    is passed:

    * **bullish** or **neutral** — allow trend-long entries without requiring an inverse hedge.
    * **defensive** (risk-off) — block trend-long entries; use the inverse / SQQQ sleeve and
      bear-ETF path instead.

    When the scorer label is missing or not one of the above, the inverse hedge is **only**
    required in a **bearish** context: breadth ``bearish_regime`` (live loop) or
    ``regime_condition`` equal to ``bearish`` (case-insensitive). Otherwise trend-long
    entries are allowed without an inverse hold.

    Returns ``(True, None)`` if the scan may run; ``(False, reason)`` if blocked.
    """
    u = (cfg or {}).get("universe") or {}
    if not bool(u.get("require_sqqq_for_trend_long_entries", False)):
        return True, None
    cond = (regime_condition or "").strip().lower() if regime_condition else ""
    if cond in ("bullish", "neutral"):
        return True, None
    if cond == "defensive":
        return (
            False,
            "trend longs off in defensive regime — use inverse / %s (bear sleeve)"
            % hedge_symbol(cfg),
        )
    if cond == "bearish" or bearish_regime:
        return allow_sqqq_logic(cfg, positions, tracked)
    return True, None


def hedge_allocation_pct(regime_score: int, cfg: dict[str, Any] | None = None) -> float:
    """
    Equity fraction for hedge target before ``max_hedge_pct`` cap in :func:`compute_hedge_size`.

    Default: score >= 2 → 10%; score < 2 → 25%. Override with YAML
    ``pct_equity_score_ge_2`` / ``pct_equity_below_2``.
    """
    v2 = (cfg or {}).get("strategy_v2") or {}
    h = v2.get("hedging") or {}
    if int(regime_score) >= 2:
        return float(h.get("pct_equity_score_ge_2", 0.10))
    return float(h.get("pct_equity_below_2", 0.25))


def compute_hedge_size(regime_score: int, equity: float, cfg: dict[str, Any] | None = None) -> float:
    """
    Target hedge **notional** (dollars).

    Uses :func:`hedge_allocation_pct` × equity, then caps by ``hedging.max_hedge_pct`` when set.
    """
    v2 = (cfg or {}).get("strategy_v2") or {}
    h = v2.get("hedging") or {}
    pct = hedge_allocation_pct(regime_score, cfg)
    eq = max(0.0, float(equity))
    raw = pct * eq
    cap = h.get("max_hedge_pct")
    if cap is not None and str(cap).strip() != "":
        raw = min(raw, float(cap) * eq)
    return max(0.0, raw)


def place_hedge_order(
    *,
    regime_score: int,
    equity: float,
    cfg: dict[str, Any],
    broker: Any,
    execution_manager: ExecutionManager,
    mid_price: float,
    spread_pct: float,
) -> tuple[Any | None, str | None]:
    """
    Size hedge from regime (same dollars as :func:`compute_hedge_size`) and submit a buy.

    Quantity is ``floor(notional / mid_price)``. Returns ``(order_or_none, error_or_none)``.
    """
    symbol = hedge_symbol(cfg)
    notional = compute_hedge_size(regime_score, equity, cfg)
    if notional <= 0:
        return None, "zero hedge target"
    if mid_price <= 0:
        return None, "invalid mid_price"
    qty = int(notional / mid_price)
    if qty < 1:
        return None, "hedge notional < 1 share at mid"
    req = execution_manager.build_order_for_entry(symbol, "buy", qty, mid_price, spread_pct)
    if req is None:
        return None, "execution could not build order (spread gate)"
    try:
        out = broker.submit_order(req)
        if out is None:
            return None, "submit returned None"
        return out, None
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])
