"""
**Replacement** preflight for a trend-long *new-name* before per-symbol dispatch.

Implements the policy that lived inline in :mod:`scripts.run_alpaca_loop` (max replacement
per cycle, weakest + strength gap + notional when at ``max_positions``).
The loop still calls :func:`src.trend_long_ranked_dispatch.dispatch_trend_long_after_buying_power`
after a successful preflight; this module only returns *skip* reasons.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.portfolio_replacement import (
    effective_signal_strength,
    replacement_max_per_cycle,
    replacement_size_ok,
    replacement_strength_gap_ok,
    symbol_long_position_market_value_usd,
    weakest_replacement_hold,
)


def preflight_replacement_gates_on_dispatch(
    *,
    port_replace: bool,  # kept for call-site compatibility; at-cap preflight is no longer gated on it
    max_port_positions: int,
    n_eligible_active: int,
    sym_u: str,
    current_position_keys: Any,
    tracked: dict[str, Any],
    eligible_active: list[str],
    positions: list[Mapping[str, Any]],
    get_bars: Any,
    engine: Any,
    rep_sub: Mapping[str, Any],
    decision_tl: Any,
    notional_tl: float,
    strength_jitter_max: float,
    replacement_threshold: float,
    allow_equal_replacement: bool,
    cycle_replacements_done: int,
) -> str | None:
    """
    When at portfolio cap and the symbol is *new*, run replacement preflight
    (weakest, strength gap, notional). Return a **skip reason** string, or
    ``None`` when dispatch may continue (or replacement does not apply).
    """
    _at_rep = (
        max_port_positions < 10**9
        and n_eligible_active >= max_port_positions
        and str(sym_u).upper() not in (current_position_keys or {})
    )
    if not _at_rep:
        return None
    _rep_max = replacement_max_per_cycle(rep_sub)
    if int(cycle_replacements_done) >= int(_rep_max):
        return "replacement skipped — max replacements per cycle (%d)" % (int(_rep_max),)
    _w_sym_pf, _w_str_pf = weakest_replacement_hold(
        dict(tracked),
        list(eligible_active),
        positions=positions,
        get_bars=get_bars,
        engine=engine,
        rep_sub=rep_sub,
    )
    if _w_sym_pf is not None and _w_sym_pf != sym_u:
        _thr_pf = float(replacement_threshold or 0.0)
        if 0.0 < _thr_pf < 1.0:
            _incoming_strength = float(
                getattr(
                    getattr(decision_tl, "entry_signal", None),
                    "strength",
                    None,
                )
                or 1.0
            )
            _incoming_strength = effective_signal_strength(
                _incoming_strength, float(strength_jitter_max)
            )
            _ok_gap, _why_gap = replacement_strength_gap_ok(
                incoming_strength=float(_incoming_strength),
                weakest_strength=float(_w_str_pf),
                threshold=float(replacement_threshold),
                allow_equal_replacement=bool(allow_equal_replacement),
                strength_jitter_max=float(strength_jitter_max),
            )
            if not _ok_gap:
                return _why_gap or "replacement strength gate"
        _weakest_mval = float(
            symbol_long_position_market_value_usd(
                list(positions), _w_sym_pf
            )
        )
        _ok_sz, _why_sz = replacement_size_ok(
            weakest_market_value_usd=_weakest_mval,
            incoming_notional_usd=float(notional_tl),
            rep_cfg=rep_sub,
        )
        if not _ok_sz:
            return _why_sz or "replacement size gate"
    return None
