"""
Wire v2 :func:`~src.strategy_v2.options_alpha.options_signal` to v1 options execution.

Requires top-level ``config["options"]`` (``long_premium_only``, allowed underlyings, etc.)
plus runtime: broker, execution manager, chain, equity, positions.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.entry_router import EntryRouteSignal, route_to_options_executor
from src.options_selector import OptionContractCandidate


def trade_options(
    symbol: str,
    *,
    config: dict[str, Any],
    broker: Any | None = None,
    execution_manager: Any | None = None,
    chain_candidates: Sequence[OptionContractCandidate] | None = None,
    underlying_spot: float | None = None,
    account_equity: float | None = None,
    positions: list[dict[str, Any]] | None = None,
    log_dt: Any | None = None,
    verbose: bool = False,
) -> bool:
    """
    Place a long-premium **call** on ``symbol`` via :func:`src.entry_router.route_to_options_executor`.

    Uses ``source="strategy_v2_independent"`` so adapters can distinguish this path from trend-long.
    Returns ``True`` only if an order was submitted successfully.
    """
    sym = str(symbol).upper()
    signal = EntryRouteSignal(
        underlying=sym,
        direction="bullish",
        source="strategy_v2_independent",
        stock_symbol=sym,
    )
    return route_to_options_executor(
        config,
        signal,
        log_dt=log_dt,
        verbose=verbose,
        account_equity=account_equity,
        positions=positions,
        broker=broker,
        execution_manager=execution_manager,
        chain_candidates=chain_candidates,
        underlying_spot=underlying_spot,
    )
