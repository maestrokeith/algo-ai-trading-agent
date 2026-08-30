"""
Portfolio construction from ranked signals (trend-long planning).

Pseudocode::

    candidates = all_signals
    sorted = sort_by_strength(candidates)
    portfolio = keep_top_N(sorted)

``all_signals`` is the union of *fresh* (symbol, strength) from this scan and
*held* eligible longs not present in fresh, using tracked ``signal_strength``
(+ optional jitter) for the latter.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from .portfolio_replacement import effective_signal_strength, tracked_signal_strength

# Default tie order when ``portfolio.priority_symbols`` is unset (live loop still passes explicitly).
DEFAULT_PRIORITY_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "NVDA", "MSFT")


def sort_by_strength_desc(pairs: Sequence[tuple[str, float]]) -> list[tuple[str, float]]:
    """Sort (symbol, strength) by strength descending; tie-break by symbol ascending."""
    return sorted(pairs, key=lambda x: (-x[1], x[0]))


def strength_rank_sort_key(
    symbol: str,
    strength: float,
    priority_symbols: Sequence[str],
) -> tuple[float, int, int, str]:
    """
    Sort key: higher *strength* first; ties favor earlier entries in *priority_symbols*,
    then symbols not in the list (alphabetically among themselves).
    """
    su = str(symbol).upper()
    st = float(strength)
    pr = [str(x).strip().upper() for x in priority_symbols if x and str(x).strip()]
    if not pr:
        return (-st, 0, 0, su)
    if su in pr:
        return (-st, 0, pr.index(su), su)
    return (-st, 1, 0, su)


def sort_by_strength_desc_priority(
    pairs: Sequence[tuple[str, float]],
    priority_symbols: Sequence[str],
) -> list[tuple[str, float]]:
    """Like :func:`sort_by_strength_desc` but ties use *priority_symbols* (then symbol name)."""
    if not priority_symbols:
        return sort_by_strength_desc(pairs)
    pr = [str(x).strip().upper() for x in priority_symbols if x and str(x).strip()]
    if not pr:
        return sort_by_strength_desc(pairs)
    return sorted(
        pairs,
        key=lambda p: strength_rank_sort_key(p[0], p[1], pr),
    )


def keep_top_n_names(ranked: Sequence[tuple[str, float]], n: int) -> list[str]:
    """First *n* symbols from an already-ranked sequence."""
    if n <= 0:
        return []
    return [s for s, _ in ranked[:n]]


def merge_fresh_signals_with_held(
    fresh_signal_strength: Sequence[tuple[str, float]],
    held_eligible_longs: Sequence[str],
    tracked: Mapping[str, object],
    strength_jitter_max: float,
) -> list[tuple[str, float]]:
    """
    ``candidates = all_signals``: fresh scan results plus held names missing from fresh,
    using :func:`tracked_signal_strength` + jitter for held-only rows.
    """
    merged: dict[str, float] = {}
    for s, st in fresh_signal_strength:
        merged[str(s).upper()] = float(st)
    held_u = {str(h).upper() for h in held_eligible_longs}
    for h in sorted(held_u):
        if h not in merged:
            row = tracked.get(h)
            if not isinstance(row, dict):
                row = {}
            merged[h] = effective_signal_strength(
                tracked_signal_strength(row),
                strength_jitter_max,
            )
    return list(merged.items())


def plan_top_n_rebalance(
    fresh_signal_strength: Sequence[tuple[str, float]],
    held_eligible_longs: Sequence[str],
    tracked: Mapping[str, object],
    max_positions: int,
    strength_jitter_max: float,
    priority_symbols: Sequence[str] | None = None,
) -> tuple[list[str], set[str], list[str]]:
    """
    Rank merged candidates, keep top ``max_positions``, return:

    * ``target_ordered`` — target portfolio symbols (strength order),
    * ``to_sell`` — held eligible longs not in target,
    * ``to_buy_ordered`` — target symbols not currently held (strength order).

    * *priority_symbols* ``None`` — use :data:`DEFAULT_PRIORITY_SYMBOLS` for equal-strength ties.
    * ``()`` — tie-break by symbol name only (legacy).
    """
    merged = merge_fresh_signals_with_held(
        fresh_signal_strength,
        held_eligible_longs,
        tracked,
        strength_jitter_max,
    )
    if priority_symbols is None:
        pri: tuple[str, ...] = DEFAULT_PRIORITY_SYMBOLS
    else:
        pri = tuple(str(x).strip().upper() for x in priority_symbols if str(x).strip())
    ranked = (
        sort_by_strength_desc(merged)
        if len(pri) == 0
        else sort_by_strength_desc_priority(merged, pri)
    )
    target_ordered = keep_top_n_names(ranked, max_positions)
    target_set = set(target_ordered)
    held_set = {str(h).upper() for h in held_eligible_longs}
    to_sell = held_set - target_set
    held_set_for_buy = held_set
    to_buy_ordered = [s for s in target_ordered if s not in held_set_for_buy]
    return target_ordered, to_sell, to_buy_ordered
