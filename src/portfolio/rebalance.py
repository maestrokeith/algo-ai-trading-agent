"""
**Rebalance-free-capital (RFC)** helpers shared by the live entry loop.

Full trim / rotate / each-cycle min-cash flows still use broker, engine, and
:class:`~src.live.exits.LiveExitContext` and remain orchestrated in
:mod:`scripts.run_alpaca_loop`; this module provides **reusable** quote/mid/position
math so the loop is not the only home for that logic. Plan/build functions stay in
:mod:`src.rebalance_free_capital`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def rfc_fallback_open_mid_from_bars(broker: Any, wsym: str) -> float:
    """One-day last close for mid fallback when the quote is thin (RFC paths)."""
    _px = 0.0
    try:
        b1 = broker.get_bars(wsym, timeframe="1Day", limit=1)
        if b1 is not None and not b1.empty:
            _px = float(b1["close"].iloc[-1])
    except Exception:
        pass
    return float(_px)


def rfc_reference_mid_for_quote(
    wquote: Any, *, fallback_1d_close: float, quote_mid: float
) -> float:
    return float(
        wquote.reference_mid(
            float(fallback_1d_close) if fallback_1d_close > 0 else float(quote_mid)
        )
    )


def rfc_effective_spread_pct(
    wquote: Any, *, stale_hint: bool, stale_quote_max_age: float, default: float = 0.15
) -> float:
    if wquote and getattr(wquote, "is_stale", None) and wquote.is_stale(
        float(stale_quote_max_age)
    ):
        return float(default)
    sp = wquote.spread_pct if wquote else None
    return float(sp if sp is not None else default)


def rfc_position_qty_floor_for_sell(
    sell_qty: float | int,
    wsym: str,
    positions: Sequence[Mapping[str, Any]],
) -> float:
    """``sell_qty`` is at most broker size; not less than the live position's abs qty."""
    out = float(sell_qty)
    for p in positions:
        if str(p.get("symbol") or "").upper() == str(wsym).upper():
            out = max(
                out,
                abs(float(p.get("qty") or 0)),
            )
            break
    return float(out)
