"""Pure helpers for smart trailing exits: unrealized %, trail activation, stop price, scale-out tiers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, MutableSet, Protocol, Sequence, runtime_checkable

EXIT_TRAILING_STOP = "exit_trailing_stop"


@runtime_checkable
class SmartExitPositionLike(Protocol):
    """Minimal position view for :func:`process_smart_exit` (``qty`` or ``quantity``)."""

    symbol: str


@dataclass
class SmartExitPositionState:
    """Mutable smart-exit state for one open long (ratchet high, scale tiers done, trail armed)."""

    entry_price: float
    high_price: float
    scaled_levels: set[float] = field(default_factory=set)
    trailing_active: bool = False


def new_smart_exit_position_state(entry_price: float) -> SmartExitPositionState:
    """Initial state: high_water starts at entry; no scales; trailing not armed."""
    ep = float(entry_price)
    return SmartExitPositionState(entry_price=ep, high_price=ep)


def bump_high_price(state: SmartExitPositionState, current_price: float) -> None:
    """Raise ``high_price`` toward the running peak (same role as ratcheted ``trail_high``)."""
    state.high_price = max(state.high_price, float(current_price))


def smart_exit_state_to_json(state: SmartExitPositionState) -> dict[str, Any]:
    """JSON-serializable dict (``scaled_levels`` stored as a sorted list)."""
    return {
        "entry_price": float(state.entry_price),
        "high_price": float(state.high_price),
        "scaled_levels": sorted(state.scaled_levels),
        "trailing_active": bool(state.trailing_active),
    }


def smart_exit_state_from_json(raw: Mapping[str, Any] | None) -> SmartExitPositionState | None:
    """Parse state written by :func:`smart_exit_state_to_json` (or equivalent YAML/JSON)."""
    if not raw:
        return None
    try:
        ep = float(raw["entry_price"])
        hp = float(raw["high_price"])
    except (KeyError, TypeError, ValueError):
        return None
    scaled: set[float] = set()
    raw_sl = raw.get("scaled_levels") or []
    if isinstance(raw_sl, (list, tuple, set)):
        for x in raw_sl:
            try:
                scaled.add(float(x))
            except (TypeError, ValueError):
                continue
    ta = bool(raw.get("trailing_active", False))
    return SmartExitPositionState(
        entry_price=ep,
        high_price=hp,
        scaled_levels=scaled,
        trailing_active=ta,
    )


def load_smart_exit_state_from_row(row: Mapping[str, Any]) -> SmartExitPositionState | None:
    """
    Load state from a tracker row.

    Uses ``row['smart_exit_state']`` when present; otherwise bootstraps from
    ``entry_price`` and optional ``trail_high`` (legacy rows).
    """
    raw = row.get("smart_exit_state")
    if isinstance(raw, dict) and raw:
        parsed = smart_exit_state_from_json(raw)
        if parsed is not None:
            return parsed
    try:
        ep = float(row.get("entry_price") or 0.0)
    except (TypeError, ValueError):
        return None
    if ep <= 0:
        return None
    st = new_smart_exit_position_state(ep)
    th = row.get("trail_high")
    if th is not None and str(th).strip() != "":
        try:
            bump_high_price(st, float(th))
        except (TypeError, ValueError):
            pass
    return st


def effective_smart_trail_pct(
    *,
    fixed_trail_pct: float,
    dynamic_enabled: bool,
    floor_pct: float,
    atr_mult: float,
    atr_pct: float | None,
) -> float:
    """
    Trailing distance in **percent** of price (same units as ``trail_pct`` in YAML).

    When ``dynamic_enabled`` and ``atr_pct`` are set, uses
    ``max(floor_pct, atr_pct * atr_mult)`` where ``atr_pct`` is (ATR/close)×100.
    Otherwise returns ``fixed_trail_pct`` (YAML ``trail_pct``).
    """
    if not dynamic_enabled or atr_pct is None:
        return float(fixed_trail_pct)
    try:
        a = float(atr_pct)
    except (TypeError, ValueError):
        return float(fixed_trail_pct)
    if a != a:  # NaN
        return float(fixed_trail_pct)
    return max(float(floor_pct), a * float(atr_mult))


def smart_trailing_cfg_for_process(
    strategy: Any,
    atr_pct: float | None = None,
) -> dict[str, Any]:
    """Build ``cfg`` for :func:`process_smart_exit` from a loaded :class:`~src.strategy.TrendFollowingStrategy`."""
    rows = [
        {"profit_pct": float(pp), "sell_pct": float(sp)}
        for pp, sp in getattr(strategy, "smart_trailing_scale_out", []) or []
    ]
    fixed = float(getattr(strategy, "smart_trailing_trail_pct", 0) or 0)
    trail = effective_smart_trail_pct(
        fixed_trail_pct=fixed,
        dynamic_enabled=bool(getattr(strategy, "smart_trailing_dynamic_trail_enabled", False)),
        floor_pct=float(getattr(strategy, "smart_trailing_dynamic_trail_floor_pct", 1.5) or 1.5),
        atr_mult=float(getattr(strategy, "smart_trailing_dynamic_trail_atr_mult", 1.2) or 1.2),
        atr_pct=atr_pct,
    )
    return {
        "activate_profit_pct": float(getattr(strategy, "smart_trailing_activate_profit_pct", 0) or 0),
        "trail_pct": trail,
        "scale_out": rows,
    }


def compute_unrealized_pct(entry_price: float, current_price: float) -> float:
    """Long unrealized return in percent (``(current - entry) / entry * 100``)."""
    return (current_price - entry_price) / entry_price * 100.0


def should_activate_trailing(pnl_pct: float, cfg: Mapping[str, Any]) -> bool:
    """True when PnL%% has reached ``cfg['activate_profit_pct']`` (trailing logic may arm)."""
    return pnl_pct >= float(cfg["activate_profit_pct"])


def compute_trailing_stop(high_price: float, cfg: Mapping[str, Any]) -> float:
    """Dynamic stop price: ``high * (1 - trail_pct/100)`` from ``cfg['trail_pct']``."""
    return float(high_price) * (1.0 - float(cfg["trail_pct"]) / 100.0)


def check_scale_out(
    pnl_pct: float,
    scale_cfg: Sequence[Mapping[str, Any]],
    already_scaled: MutableSet[float],
) -> list[Mapping[str, Any]]:
    """
    Return scale-out rows that are eligible at ``pnl_pct`` and not yet in ``already_scaled``.

    Each ``level`` must include ``profit_pct`` (and typically ``sell_pct``). After execution,
    callers should add ``level['profit_pct']`` (as ``float``) to ``already_scaled``.
    """
    actions: list[Mapping[str, Any]] = []
    for level in scale_cfg:
        pp = float(level["profit_pct"])
        if pnl_pct >= pp and pp not in already_scaled:
            actions.append(level)
    return actions


def _position_qty(position: Any) -> int:
    if hasattr(position, "qty"):
        return int(position.qty)
    return int(getattr(position, "quantity", 0))


def process_smart_exit(
    position: SmartExitPositionLike | Any,
    price: float,
    cfg: Mapping[str, Any],
    state: SmartExitPositionState,
    *,
    sell: Callable[[str, int], bool | None],
    sell_all: Callable[[str], bool | None],
) -> Literal["exit_trailing_stop"] | None:
    """
    One evaluation cycle: ratchet high, arm trailing, optional scale-outs, then trailing stop.

    Mutates ``state`` in place. Invokes ``sell(symbol, qty)`` for each scale tier and
    ``sell_all(symbol)`` on trailing exit. Uses remaining quantity after each partial so
    multiple ``scale_out`` rows in one pass use a shrinking base (unlike a fixed ``qty`` snapshot).

    If ``sell`` or ``sell_all`` returns ``False`` (e.g. per-cycle action cap), state updates for
    that tier stop and trailing is not taken this pass. ``None`` / other returns preserve legacy
    behavior (treat as success for state bookkeeping).
    """
    sym = str(getattr(position, "symbol"))
    fp = float(price)
    pnl_pct = compute_unrealized_pct(float(state.entry_price), fp)
    bump_high_price(state, fp)

    if not state.trailing_active and should_activate_trailing(pnl_pct, cfg):
        state.trailing_active = True

    scale_cfg = cfg.get("scale_out") or []
    if not isinstance(scale_cfg, list):
        scale_cfg = []
    actions = check_scale_out(pnl_pct, scale_cfg, state.scaled_levels)
    working_qty = _position_qty(position)
    for act in actions:
        try:
            sp = float(act["sell_pct"])
            pp = float(act["profit_pct"])
        except (KeyError, TypeError, ValueError):
            continue
        qty_to_sell = int(working_qty * sp)
        qty_to_sell = max(0, min(qty_to_sell, working_qty))
        if qty_to_sell > 0:
            out = sell(sym, qty_to_sell)
            if out is False:
                break
            working_qty -= qty_to_sell
            state.scaled_levels.add(pp)

    if state.trailing_active:
        stop_price = compute_trailing_stop(state.high_price, cfg)
        if fp < stop_price:
            out_all = sell_all(sym)
            if out_all is not False:
                return EXIT_TRAILING_STOP
    return None
