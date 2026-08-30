"""
Strategy router: choose **options** (long premium contract under budget) vs **shares**.

Thin orchestration over option intent adaptation and ranked budget selection; execution stays in
:func:`src.entry_router.route_to_options_executor`.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from src.options_adapter import adapt_stock_signal_to_option_intent
from src.options_premium_risk import holding_equity_long_for_underlying
from src.portfolio_allocation import capital_split_stock_option_fracs
from src.options_selector import (
    OptionContractCandidate,
    SelectedOptionContract,
    select_first_ranked_candidate_within_budget,
)


@dataclass(frozen=True)
class StrategyRouteOutcome:
    """Result of :func:`route_options_or_shares`."""

    leg: Literal["options", "shares"]
    option_contract: SelectedOptionContract | None
    share_size: int
    """Populated when ``leg == "shares"`` (including when options were on but no contract fit)."""
    options_select_error: str | None = None
    """Set when options were eligible but no contract was found (for logging)."""


def find_option_under_budget(
    config: dict[str, Any],
    signal: Any,
    *,
    chain_candidates: Sequence[OptionContractCandidate] | None,
    underlying_spot: float | None,
    equity: float | None,
    positions: list[dict[str, Any]] | None,
    as_of: date,
    premium_budget_cap_usd: float | None = None,
    tracked: Mapping[str, Any] | None = None,
) -> tuple[SelectedOptionContract | None, str | None]:
    """
    Select a single long-premium contract within premium budget and spread/liquidity caps.

    ``signal`` must expose ``underlying``, ``direction``, ``source``, ``stock_symbol`` (same as
    :class:`~src.entry_router.EntryRouteSignal`).
    """
    u0 = str(getattr(signal, "underlying", None) or "").strip().upper()
    if u0 and holding_equity_long_for_underlying(u0, positions, tracked):
        return None, "holding equity; skip option overlay"
    opts = config.get("options") or {}
    if bool(opts.get("enabled")) and str(opts.get("mode") or "").strip().lower() in (
        "long_premium_only",
        "paper_only",
        "shadow_live",
        "live",
        "live_long_premium",
    ):
        allowed = {str(x).upper() for x in (opts.get("allowed_underlyings") or [])}
        u = str(getattr(signal, "underlying", None) or "").strip().upper()
        if not allowed or u not in allowed:
            return None, "underlying not allowed for options"

    intent, adapt_err = adapt_stock_signal_to_option_intent(
        config,
        underlying=getattr(signal, "underlying", None),
        direction=getattr(signal, "direction", None),
        source=getattr(signal, "source", None),
        stock_symbol=getattr(signal, "stock_symbol", None),
    )
    if intent is None:
        return None, adapt_err or "could not build option intent"

    if equity is None or positions is None:
        return None, "missing account_equity or positions for ranked selection and premium caps"

    _, _om = capital_split_stock_option_fracs(config)
    equity_eff = float(equity) * float(_om)
    cap_eff = premium_budget_cap_usd
    if cap_eff is not None:
        try:
            cap_eff = float(cap_eff) * float(_om)
        except (TypeError, ValueError):
            cap_eff = None

    selected, sel_err = select_first_ranked_candidate_within_budget(
        config,
        intent_underlying=intent.underlying,
        intent_right=intent.right,
        chain=chain_candidates,
        underlying_spot=underlying_spot,
        equity=equity_eff,
        positions=positions,
        as_of=as_of,
        signal=signal,
        expiries=None,
        strikes=None,
        premium_budget_cap_usd=cap_eff,
    )
    return selected, sel_err


def route_options_or_shares(
    share_size: int,
    *,
    options_enabled: bool,
    config: dict[str, Any],
    signal: Any,
    chain_candidates: Sequence[OptionContractCandidate] | None,
    underlying_spot: float | None,
    equity: float | None,
    positions: list[dict[str, Any]] | None,
    as_of: date,
    tracked: Mapping[str, Any] | None = None,
) -> StrategyRouteOutcome:
    """
    If ``options_enabled``, try :func:`find_option_under_budget`; on success return the contract.

    Otherwise return the equity leg with ``share_size`` (caller-sized shares). This keeps strong
    stock entries alive when options routing is enabled but no acceptable contract is available.
    """
    if options_enabled:
        contract, err = find_option_under_budget(
            config,
            signal,
            chain_candidates=chain_candidates,
            underlying_spot=underlying_spot,
            equity=equity,
            positions=positions,
            as_of=as_of,
            tracked=tracked,
        )
        if contract is not None:
            return StrategyRouteOutcome(
                leg="options",
                option_contract=contract,
                share_size=0,
                options_select_error=None,
            )
        if str((config.get("options") or {}).get("mode") or "").strip().lower() == "paper_only":
            return StrategyRouteOutcome(
                leg="options",
                option_contract=None,
                share_size=0,
                options_select_error=err,
            )
        # Explicit stock fallback: preserve the sized equity order when options are enabled but
        # contract selection fails on budget, spread, liquidity, or eligibility.
        return StrategyRouteOutcome(
            leg="shares",
            option_contract=None,
            share_size=int(share_size),
            options_select_error=err,
        )
    return StrategyRouteOutcome(
        leg="shares",
        option_contract=None,
        share_size=int(share_size),
        options_select_error=None,
    )
