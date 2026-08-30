"""
Cap contracts by premium budget and place option orders.

Uses options_premium_risk for portfolio + per-trade premium caps; uses
ExecutionManager.build_order + broker.submit_order for the actual send.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, TYPE_CHECKING, Sequence

from .options_config import allow_new_entries, max_open_option_positions_cap
from .options_premium_risk import (
    count_open_long_option_positions,
    evaluate_options_order_risk_controls,
)
from .options_selector import (
    OptionContractCandidate,
    SelectedOptionContract,
    candidate_to_selected_contract,
    lower_strike_candidates_same_series,
)

if TYPE_CHECKING:
    from .execution import ExecutionManager


def _max_lower_strike_attempts(config: dict[str, Any]) -> int:
    o = config.get("options") or {}
    raw = o.get("premium_over_budget_max_lower_strike_attempts")
    if raw is None or str(raw).strip() == "":
        return 200
    try:
        return max(1, min(2000, int(raw)))
    except (TypeError, ValueError):
        return 200


@dataclass(frozen=True)
class PreparedOptionOrder:
    occ_symbol: str
    contracts: int
    mid: float
    spread_pct: float
    bid: float | None = None
    ask: float | None = None


def max_open_option_positions_limit(config: dict[str, Any]) -> int:
    return int(max_open_option_positions_cap(config))


def prepare_option_order_premium_only(
    config: dict[str, Any],
    *,
    equity: float,
    positions: list[dict[str, Any]] | None,
    selected: SelectedOptionContract,
) -> tuple[PreparedOptionOrder | None, str | None]:
    """
    Apply max open positions + v1 premium-budget contract count; return prepared order or (None, reason).
    """
    opts = config.get("options") or {}
    if not bool(opts.get("enabled")):
        return None, "options disabled"
    if not allow_new_entries(config):
        return None, "options new entries disabled"

    max_open = max_open_option_positions_limit(config)
    if count_open_long_option_positions(positions) >= max_open:
        return None, "max open option positions (%d)" % max_open

    gate = evaluate_options_order_risk_controls(
        config,
        equity=float(equity),
        positions=positions,
        option_mid_price=float(selected.mid),
    )
    if not gate.ok:
        return None, gate.reason

    return PreparedOptionOrder(
        occ_symbol=selected.symbol,
        contracts=int(gate.contracts),
        mid=float(selected.mid),
        spread_pct=float(selected.spread_pct),
        bid=float(selected.bid),
        ask=float(selected.ask),
    ), None


def prepare_option_order_premium_only_with_lower_strike_fallback(
    config: dict[str, Any],
    *,
    equity: float,
    positions: list[dict[str, Any]] | None,
    chain_candidates: Sequence[OptionContractCandidate],
    selected_atm: SelectedOptionContract,
    intent_underlying: str,
    intent_right: str,
    as_of: date | None = None,
) -> tuple[PreparedOptionOrder | None, SelectedOptionContract | None, str | None]:
    """
    Run :func:`prepare_option_order_premium_only` on the ATM pick; if premium exceeds budget
    (``premium too expensive``), optionally walk **lower strikes** (same expiry / right) until
    one fits or the ladder ends.

    Controlled by ``options.premium_over_budget_try_lower_strike`` (default ``True``).
    """
    prep, err = prepare_option_order_premium_only(
        config,
        equity=float(equity),
        positions=positions,
        selected=selected_atm,
    )
    if prep is not None:
        return prep, selected_atm, None

    opts = config.get("options") or {}
    if not bool(opts.get("premium_over_budget_try_lower_strike", True)):
        return None, selected_atm, err

    reason = str(err or "")
    if "premium too expensive" not in reason:
        return None, selected_atm, err

    want = str(intent_right or "").strip().lower()
    if want in ("calls", "call"):
        want = "call"
    elif want in ("puts", "put"):
        want = "put"
    else:
        return None, selected_atm, err

    alts = lower_strike_candidates_same_series(
        config,
        str(intent_underlying or "").strip().upper(),
        want,
        chain_candidates,
        selected_atm,
        as_of=as_of,
        max_candidates=_max_lower_strike_attempts(config),
    )
    for c in alts:
        sel, _lq = candidate_to_selected_contract(config, c, want)
        if sel is None:
            continue
        prep2, err2 = prepare_option_order_premium_only(
            config,
            equity=float(equity),
            positions=positions,
            selected=sel,
        )
        if prep2 is not None:
            return prep2, sel, None
        if err2 is not None and "premium too expensive" not in str(err2):
            return None, selected_atm, err2

    return None, selected_atm, err


def place_option_order(
    broker: Any,
    execution: "ExecutionManager",
    prepared: PreparedOptionOrder,
) -> tuple[Any | None, float | None, str | None]:
    """Build a limit order via ExecutionManager and submit on broker."""
    req = execution.build_order_for_entry(
        prepared.occ_symbol,
        "buy",
        prepared.contracts,
        prepared.mid,
        prepared.spread_pct,
        bid=prepared.bid,
        ask=prepared.ask,
    )
    if req is None:
        return None, None, "execution could not build order (spread gate)"
    try:
        out = broker.submit_order(req)
        limit_price = float(req.limit_price) if getattr(req, "limit_price", None) is not None else None
        return out, limit_price, None
    except Exception as e:
        return None, None, "%s: %s" % (type(e).__name__, str(e)[:120])
