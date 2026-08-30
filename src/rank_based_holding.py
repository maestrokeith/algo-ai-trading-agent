"""
Rank-based stock book: keep the top *N* positions by :func:`replacement_hold_strength`,
fully exit the rest (see ``portfolio.rank_based_holding`` in YAML).

Pseudocode::

    ranked = rank_positions()
    keep, sell_rest = keep_top_n_sell_rest(ranked, top_n=10)  # keep = ranked[:10]
    # live pass sells symbols in *sell_rest* (worst-ranked first), subject to min-hold / gates.

Legacy compatibility module: the live orchestration layer now lives in
``src/live/rank_based_holding.py`` and imports these helpers.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.portfolio_replacement import replacement_hold_strength


def rank_positions(
    eligible_symbols: list[str],
    tracked: dict[str, Any],
    positions: list[dict[str, Any]],
    *,
    get_bars: Callable[[str], Any],
    engine: Any,
    rep_sub: Mapping[str, Any] | None = None,
) -> list[str]:
    """
    All *eligible* long **stock** symbols, sorted **best first** (higher
    :func:`~src.portfolio_replacement.replacement_hold_strength` first; tie-break: symbol A–Z).
    """
    scored: list[tuple[str, float]] = []
    for sym in eligible_symbols:
        su = str(sym or "").strip().upper()
        if not su:
            continue
        sc = replacement_hold_strength(
            su,
            tracked,
            positions,
            get_bars=get_bars,
            engine=engine,
            rep_sub=rep_sub,
        )
        scored.append((su, float(sc)))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return [s for s, _ in scored]


def keep_top_n_sell_rest(
    ranked_best_first: Sequence[str],
    *,
    top_n: int,
) -> tuple[list[str], list[str]]:
    """
    ``keep = ranked[:top_n]``, ``sell_rest = ranked[top_n:]`` (as lists).

    *ranked_best_first* should be the result of :func:`rank_positions` (best name first).
    """
    n = int(top_n)
    if n < 0:
        n = 0
    best = list(ranked_best_first)
    return best[:n], best[n:]


def sell_rest_worst_first(sell_rest: Sequence[str]) -> list[str]:
    """
    Order for full exits: **worst** held names first (end of the strength ordering).

    *sell_rest* is the tail from :func:`keep_top_n_sell_rest` (i.e. ``ranked[top_n:]`` from best→worst
    within the tail, so the **weakest** overall is the **last** element of *sell_rest*).
    """
    return list(reversed(list(sell_rest)))


def parse_rank_based_holding_cfg(portfolio: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    Read ``portfolio.rank_based_holding``:

    * **enabled** — when true, the live loop may full-exit names outside the top *N* (default false).
    * **top_n** — keep this many (0 = use *max_portfolio_positions* from the caller).
    * **max_sells_per_pass** — cap discretionary full exits per exit heartbeat (default 2).
    """
    raw = (dict(portfolio or {}).get("rank_based_holding") or {})
    if not isinstance(raw, dict):
        raw = {}
    top_n = raw.get("top_n")
    try:
        top_n_i = int(top_n) if top_n is not None and str(top_n).strip() != "" else 0
    except (TypeError, ValueError):
        top_n_i = 0
    try:
        msp = int(raw.get("max_sells_per_pass", 2) or 2)
    except (TypeError, ValueError):
        msp = 2
    msp = max(0, msp)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "top_n": max(0, top_n_i),
        "max_sells_per_pass": msp,
    }
