"""
Boost position size for the best-ranked names in a trend-long batch (``top_signals``).

When ``position_sizing.winner_allocation`` is enabled, the first *top_n* rows in the ranked
``chosen`` list (best-first) get a multiplier on shares / notional and the buy order is rebuilt.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any


def parse_winner_allocation_config(config: Mapping[str, Any] | None) -> tuple[bool, int, float]:
    """
    Return ``(enabled, top_n, size_multiplier)`` from ``config["position_sizing"]["winner_allocation"]``.

    *top_n* = how many of the best-ranked *chosen* rows count as *top signals*;
    *size_multiplier* = factor applied to shares (and derived notional / risk fields).
    """
    cfg = config or {}
    ps = cfg.get("position_sizing")
    if not isinstance(ps, dict):
        ps = {}
    w = ps.get("winner_allocation")
    if not isinstance(w, dict):
        w = {}
    try:
        en = bool(w.get("enabled", False))
    except (TypeError, ValueError):
        en = False
    try:
        top_n = max(0, int(w.get("top_n", 0) or 0))
    except (TypeError, ValueError):
        top_n = 0
    try:
        mult = float(w.get("size_multiplier", 1.5) or 1.0)
    except (TypeError, ValueError):
        mult = 1.0
    if mult < 1.0 + 1e-12:
        en = False
    if en and top_n == 0:
        top_n = 1
    return en, top_n, mult


def mark_top_signal_symbols_in_chosen(
    chosen: Sequence[dict[str, Any]],
    *,
    top_n: int,
    size_multiplier: float,
    sym_key: str = "sym_u",
) -> set[str]:
    """
    For the first *top_n* rows in *chosen* (already best-first), set
    ``row["winner_size_multiplier"]`` and ``row["in_top_signals"] = True``,
    and return the set of upper symbols (``top_signals``) for that batch.
    """
    if top_n <= 0 or not chosen or size_multiplier <= 1.0 + 1e-12:
        return set()
    n = min(int(top_n), len(chosen))
    top_syms: set[str] = set()
    m = float(size_multiplier)
    for i, row in enumerate(chosen):
        if i >= n:
            break
        if not isinstance(row, dict):
            continue
        su = str(row.get(sym_key) or "").strip().upper()
        if not su:
            continue
        top_syms.add(su)
        row["in_top_signals"] = True
        row["winner_size_multiplier"] = m
    return top_syms


def apply_winner_size_multiplier_to_trend_row(
    row_tl: dict[str, Any],
    *,
    engine: Any,
) -> None:
    """
    If ``row_tl["winner_size_multiplier"] > 1``, scale ``position_sizing``, ``notional``, and
    rebuild the stock buy ``order_request`` for live dispatch.
    """
    m = float(row_tl.get("winner_size_multiplier") or 1.0)
    if m <= 1.0 + 1e-12:
        return
    d = row_tl.get("decision")
    if d is None or not getattr(d, "allowed", False):
        return
    ps = getattr(d, "position_sizing", None)
    if ps is None:
        return
    try:
        sh0 = int(getattr(ps, "shares", 0) or 0)
    except (TypeError, ValueError):
        sh0 = 0
    if sh0 < 1:
        return
    new_sh = max(1, int(round(sh0 * m)))
    if new_sh == sh0:
        return
    r = new_sh / float(sh0)
    n0 = float(getattr(ps, "notional", 0) or 0.0)
    new_ps = replace(
        ps,
        shares=new_sh,
        notional=n0 * r,
        risk_amount=float(getattr(ps, "risk_amount", 0) or 0.0) * r,
        risk_pct=float(getattr(ps, "risk_pct", 0) or 0.0) * r,
        exposure_pct=float(getattr(ps, "exposure_pct", 0) or 0.0) * r,
    )
    df = row_tl.get("df")
    q = row_tl.get("quote")
    mid = 0.0
    if df is not None and not getattr(df, "empty", True):
        try:
            mid = float(df["close"].iloc[-1])
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            mid = 0.0
    spct = 0.15
    if q is not None and getattr(q, "spread_pct", None) is not None:
        try:
            spct = float(q.spread_pct)
        except (TypeError, ValueError):
            spct = 0.15
    ig = bool(getattr(q, "skip_spread_check", False)) if q is not None else False
    sym = str(row_tl.get("sym_u", "")).strip()
    oreq = engine.execution.build_order_for_entry(
        sym,
        "buy",
        int(new_sh),
        float(mid),
        float(spct),
        tick_size=0.01,
        ignore_spread_gate=ig,
        bid=float(q.bid) if q is not None and getattr(q, "bid", None) is not None else None,
        ask=float(q.ask) if q is not None and getattr(q, "ask", None) is not None else None,
        notional=float(new_ps.notional),
        cap_relax_factor=1.0,
    )
    if oreq is None:
        return
    d.position_sizing = new_ps
    d.order_request = oreq
    row_tl["notional"] = float(new_ps.notional)
