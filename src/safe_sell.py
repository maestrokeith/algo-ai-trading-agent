"""Share-safe sells: clamp to inventory minus qty reserved in open sell orders.

Matches the usual rule::

    qty = min(position.qty_available, requested_qty)

Here **effective** ``qty_available`` is broker position size minus quantity already committed
in open **sell** orders (see :func:`available_sell_qty_shares`). Equivalent to::

    available = max(float(pos.qty) - held_open_sell_qty, 0)
    sell_qty = min(requested_qty, available)
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, Mapping, Sequence
from unittest.mock import Mock

from src.execution import OrderRequest
from src.loop_helpers import broker_available_qty_for_symbol
from src.portfolio.rebalance import rfc_position_qty_floor_for_sell

log = logging.getLogger(__name__)

FULL_EXIT_REASONS = {
    "stop_loss",
    "hard_exit",
    "weak_exit",
    "reduce_to_zero",
    "stale_exit",
    "end_of_day_exit",
    "dynamic_eod_flatten",
    "trailing_stop",
    "trail",
    "time_bars",
    "signal_exit",
    "kill_switch",
}


def _order_side_lower(order: Any) -> str:
    if isinstance(order, dict):
        return str(order.get("side") or "").strip().lower()
    return str(getattr(order, "side", "") or "").strip().lower()


def _order_symbol_upper(order: Any) -> str:
    if isinstance(order, dict):
        return str(order.get("symbol") or "").strip().upper()
    return str(getattr(order, "symbol", "") or "").strip().upper()


def _order_qty_float(order: Any) -> float:
    if isinstance(order, dict):
        try:
            return float(order.get("qty", 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(getattr(order, "qty", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def open_sell_orders_held_qty(broker: Any, symbol: str) -> float:
    """
    Sum of ``qty`` on open orders for *symbol* with side **sell** (held against position).
    """
    su = str(symbol or "").strip().upper()
    if not su:
        return 0.0
    get_fn = getattr(broker, "get_open_orders", None)
    if not callable(get_fn):
        return 0.0
    try:
        rows = get_fn() or []
    except Exception:
        log.warning(
            "open_sell_orders_held_qty: get_open_orders failed for symbol=%s",
            su,
            exc_info=True,
        )
        return 0.0
    held = 0.0
    for o in rows:
        if _order_symbol_upper(o) != su:
            continue
        if _order_side_lower(o) != "sell":
            continue
        held += max(0.0, _order_qty_float(o))
    return held


def available_sell_qty_shares(broker: Any, symbol: str) -> tuple[float, float, float]:
    """
    ``(position_qty, held_by_open_sells, available)`` with
    ``available = max(position_qty - held_by_open_sells, 0)``.
    """
    available_fn = getattr(broker, "available_position_qty", None)
    if callable(available_fn) and (
        not isinstance(available_fn, Mock) or "available_position_qty" in getattr(broker, "__dict__", {})
    ):
        try:
            pos_qty, held, available = available_fn(symbol)
            return (
                max(0.0, float(pos_qty)),
                max(0.0, float(held)),
                max(0.0, float(available)),
            )
        except Exception:
            log.warning(
                "available_sell_qty_shares: available_position_qty failed for symbol=%s",
                str(symbol or "").strip().upper(),
                exc_info=True,
            )
    pos_qty = float(broker_available_qty_for_symbol(broker, symbol))
    held = float(open_sell_orders_held_qty(broker, symbol))
    avail = max(pos_qty - held, 0.0)
    return pos_qty, held, avail


def clamp_sell_qty_for_open_orders(
    broker: Any, symbol: str, target_qty: float | int
) -> float:
    """
    ``min(requested_qty, available)``, or ``0`` if nothing to sell.

    When the request would leave a fractional tail under one share, return the full
    available quantity so final liquidation includes the decimal shares too.

    *available* is from :func:`available_sell_qty_shares` (position qty net of open sells).
    """
    _, _, avail = available_sell_qty_shares(broker, symbol)
    try:
        t = float(target_qty)
    except (TypeError, ValueError):
        return 0
    clipped = min(t, avail)
    remaining = avail - clipped
    if clipped >= avail or 0.0 < remaining < 1.0:
        return float(avail)
    q = float(int(clipped))
    return q if q >= 1.0 else 0.0


def build_safe_sell_order_request(
    broker: Any,
    execution: Any,
    symbol: str,
    target_qty: float | int,
    *,
    mid_price: float,
    spread_pct: float,
    ignore_spread_gate: bool = False,
    bid: float | None = None,
    ask: float | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
) -> OrderRequest | None:
    """
    Clamp *target_qty* to inventory minus open sell commitment, then ``execution.build_order``.

    Returns ``None`` when no shares are available (logs skip) or the spread gate blocks.
    """
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return None
    try:
        requested = max(0.0, float(target_qty))
    except (TypeError, ValueError):
        requested = 0.0
    existing_qty, held_for_orders, available_qty = available_sell_qty_shares(broker, sym_u)
    if available_qty <= 0.0:
        log.info(
            "EXIT_SKIP_HELD_FOR_ORDERS symbol=%s existing_qty=%.9g held_for_orders=%.9g available=%.9g",
            sym_u,
            existing_qty,
            held_for_orders,
            available_qty,
        )
        return None
    sell_qty = min(requested, available_qty)
    if requested > available_qty:
        log.info(
            "EXIT_QTY_CLAMP symbol=%s requested=%.9g available=%.9g final_qty=%.9g",
            sym_u,
            requested,
            available_qty,
            sell_qty,
        )
    remaining_available = available_qty - sell_qty
    if sell_qty >= available_qty or 0.0 < remaining_available < 1.0:
        sell_qty = float(available_qty)
    else:
        sell_qty = float(int(sell_qty))
    if sell_qty <= 0.0:
        log.info(
            "EXIT_SKIP_HELD_FOR_ORDERS symbol=%s existing_qty=%.9g held_for_orders=%.9g available=%.9g",
            sym_u,
            existing_qty,
            held_for_orders,
            available_qty,
        )
        return None
    pos_rows = list(positions) if positions is not None else []
    if sell_qty >= available_qty:
        _pqx = float(available_qty)
    else:
        _pqx = rfc_position_qty_floor_for_sell(sell_qty, sym_u, pos_rows)
    return execution.build_order(
        sym_u,
        "sell",
        sell_qty,
        float(mid_price),
        float(spread_pct),
        ignore_spread_gate=ignore_spread_gate,
        bid=bid,
        ask=ask,
        position_qty=_pqx,
        notional=None,
    )


def is_full_exit_reason(reason: Any) -> bool:
    text = str(reason or "").strip().lower()
    if not text:
        return False
    text = text.replace("-", "_").replace(" ", "_")
    return text in FULL_EXIT_REASONS or any(token in text for token in FULL_EXIT_REASONS)


def dust_cleanup_threshold_usd(config: Mapping[str, Any] | None) -> float:
    cfg = config if isinstance(config, Mapping) else {}
    containers = (
        cfg.get("execution") if isinstance(cfg.get("execution"), Mapping) else {},
        cfg.get("portfolio") if isinstance(cfg.get("portfolio"), Mapping) else {},
        cfg.get("dust_cleanup") if isinstance(cfg.get("dust_cleanup"), Mapping) else {},
    )
    for container in containers:
        for key in ("dust_cleanup_threshold_usd", "dust_position_threshold_usd", "min_dust_market_value"):
            if key not in container:
                continue
            try:
                value = float(container.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            return max(0.0, value)
    return 100.0


def submit_fractional_full_close(
    broker: Any,
    symbol: str,
    *,
    reason: str,
    prefer_close_position: bool = True,
) -> Any | None:
    """Submit a full position close using exact broker quantity, not notional."""
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return None
    position_qty, held_for_orders, available_qty = available_sell_qty_shares(broker, sym_u)
    if available_qty <= 0.0:
        log.info(
            "DUST_CLEANUP_SKIPPED symbol=%s reason=open_sell_order" if held_for_orders > 0.0 else "DUST_CLEANUP_SKIPPED symbol=%s reason=no_available_qty",
            sym_u,
        )
        return None
    log.info(
        "FRACTIONAL_FULL_CLOSE symbol=%s qty=%.9g reason=%s",
        sym_u,
        float(available_qty),
        str(reason or "full_exit"),
    )
    if prefer_close_position and held_for_orders <= 0.0:
        close_fn = getattr(broker, "close_position", None)
        if callable(close_fn):
            return close_fn(sym_u)
    submit_fn = getattr(broker, "submit_market_sell", None)
    if callable(submit_fn):
        return submit_fn(sym_u, float(available_qty))
    from src.execution import OrderRequest, OrderType

    submit_order = getattr(broker, "submit_order", None)
    if callable(submit_order):
        return submit_order(
            OrderRequest(
                symbol=sym_u,
                side="sell",
                quantity=float(available_qty),
                order_type=OrderType.MARKET,
                notional=None,
            )
        )
    return None


def maybe_submit_dust_cleanup(
    broker: Any,
    symbol: str,
    *,
    market_value: Any,
    config: Mapping[str, Any] | None = None,
    protected_symbols: Iterable[str] | None = None,
    active_intent_symbols: Iterable[str] | None = None,
) -> Any | None:
    """Close tiny non-protected broker remnants when no hold/buy/sell intent is active."""
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return None
    threshold = dust_cleanup_threshold_usd(config)
    if threshold <= 0.0:
        return None
    try:
        mv = abs(float(market_value or 0.0))
    except (TypeError, ValueError):
        mv = 0.0
    if mv <= 0.0 or mv >= threshold:
        return None
    log.info(
        "DUST_POSITION_DETECTED symbol=%s market_value=%.2f threshold=%.2f",
        sym_u,
        float(mv),
        float(threshold),
    )
    protected = {str(s or "").strip().upper() for s in protected_symbols or [] if str(s or "").strip()}
    if sym_u in protected:
        log.info("DUST_CLEANUP_SKIPPED symbol=%s reason=protected_position", sym_u)
        return None
    active = {str(s or "").strip().upper() for s in active_intent_symbols or [] if str(s or "").strip()}
    if sym_u in active:
        log.info("DUST_CLEANUP_SKIPPED symbol=%s reason=active_buy_or_hold_intent", sym_u)
        return None
    position_qty, held_for_orders, available_qty = available_sell_qty_shares(broker, sym_u)
    if position_qty <= 0.0 or available_qty <= 0.0:
        reason = "open_sell_order" if held_for_orders > 0.0 else "no_available_qty"
        log.info("DUST_CLEANUP_SKIPPED symbol=%s reason=%s", sym_u, reason)
        return None
    order = submit_fractional_full_close(
        broker,
        sym_u,
        reason="dust_cleanup",
        prefer_close_position=True,
    )
    if order is not None:
        log.info(
            "DUST_CLEANUP_ORDER_SUBMITTED symbol=%s qty=%.9g",
            sym_u,
            float(available_qty),
        )
    return order


def safe_sell(
    broker: Any,
    symbol: str,
    target_qty: float | int,
    *,
    execution: Any,
    mid_price: float,
    spread_pct: float,
    ignore_spread_gate: bool = False,
    bid: float | None = None,
    ask: float | None = None,
    positions: Sequence[Mapping[str, Any]] | None = None,
) -> bool:
    """
    Clamp, ``build_order``, then ``broker.submit_order`` (same as successful
    :func:`build_safe_sell_order_request` + submit).
    """
    req = build_safe_sell_order_request(
        broker,
        execution,
        symbol,
        target_qty,
        mid_price=mid_price,
        spread_pct=spread_pct,
        ignore_spread_gate=ignore_spread_gate,
        bid=bid,
        ask=ask,
        positions=positions,
    )
    if not req:
        return False
    broker.submit_order(req)
    return True
