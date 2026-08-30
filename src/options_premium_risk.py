"""
Options risk limits in **premium paid** (debit), not stock notional.

Sizing (v2 default):
  risk_dollars = equity × ``options.max_premium_pct_of_equity`` (fraction, e.g. ``0.02``) or legacy
  ``options.risk_per_trade_pct`` / 100; optional ``options.max_premium_per_trade`` hard $ cap.
  premium per contract = option_mid × 100
  contracts = floor(min(risk_dollars, per-order ceiling, room under total cap) / premium_per_contract)

``max_option_position_pct`` caps premium dollars per order as % of equity (min with risk budget).
``v1_max_contracts_per_trade`` is an optional hard cap on contracts (omit or very large for risk-only sizing).
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .options_config import max_premium_frac_of_equity, max_premium_per_trade_usd
from .portfolio_allocation import effective_options_total_cap_frac
from .position_tracker import tracked_row_has_open_long

# OCC-style US option symbol: root + YYMMDD + C|P + 8-digit strike (thousandths)
_OCC_OPTION_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")


def is_option_symbol(symbol: str) -> bool:
    s = str(symbol or "").strip().upper().replace(" ", "")
    if not s or len(s) < 12:
        return False
    return bool(_OCC_OPTION_RE.match(s))


def is_option_position(position: dict[str, Any] | None) -> bool:
    """True if *position* is a broker-style row whose symbol is an OCC option."""
    if not position:
        return False
    return is_option_symbol(str(position.get("symbol") or ""))


def holding_equity_long_for_underlying(
    symbol_upper: str,
    positions: Sequence[Mapping[str, Any]] | None,
    tracked: Mapping[str, Any] | None,
) -> bool:
    """
    True if the account already has a long **equity** line in *symbol_upper* (not an OCC option).

    Used to avoid opening a long option on the same underlying while stock is held.
    """
    su = str(symbol_upper or "").strip().upper()
    if not su:
        return False
    if isinstance(tracked, dict) and tracked_row_has_open_long(tracked.get(su)):
        return True
    if not positions:
        return False
    for p in positions:
        sym = str(p.get("symbol") or "").strip().upper()
        if not sym or is_option_symbol(sym):
            continue
        if sym != su:
            continue
        try:
            q = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            q = 0
        if q > 0:
            return True
    return False


def sum_open_option_positions_premium(positions: list[dict[str, Any]] | None) -> float:
    """
    Sum premium paid for open **long** option positions (debit cost basis).

    Uses abs(cost_basis) when qty > 0 and symbol looks like an OCC option.
    """
    if not positions:
        return 0.0
    total = 0.0
    for p in positions:
        sym = str(p.get("symbol") or "")
        if not is_option_symbol(sym):
            continue
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            continue
        try:
            cb = float(p.get("cost_basis") or 0)
        except (TypeError, ValueError):
            cb = 0.0
        total += abs(cb)
    return total


def count_open_long_option_positions(positions: list[dict[str, Any]] | None) -> int:
    """Number of distinct long option positions (OCC symbol, qty > 0)."""
    if not positions:
        return 0
    n = 0
    for p in positions:
        sym = str(p.get("symbol") or "")
        if not is_option_symbol(sym):
            continue
        try:
            qty = int(float(p.get("qty") or 0))
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            n += 1
    return n


def _max_single_option_position_frac(raw: Any) -> float:
    """Fraction of equity for single-order premium cap; same convention as ``total_exposure_limit``."""
    if raw is None or str(raw).strip() == "":
        return 1.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if v <= 0:
        return 1.0
    # Values > 1 are percent points (5 → 5%%); (0, 1] are fractions (0.02 → 2%%).
    single = v / 100.0 if v > 1.0 else v
    return max(0.0, min(1.0, single))


def _options_pct_limits(config: dict[str, Any]) -> tuple[float, float]:
    o = config.get("options") or {}
    max_total = effective_options_total_cap_frac(config)
    max_single = _max_single_option_position_frac(o.get("max_option_position_pct", 100))
    return max_total, max_single


def max_premium_budget_usd(
    config: dict[str, Any],
    *,
    equity: float,
    positions: list[dict[str, Any]] | None,
) -> tuple[float, str | None]:
    """
    Dollar premium budget for one new order (``min`` of risk %, single-name %, room under total cap,
    ``max_premium_per_trade``) **before** dividing by per-contract cost.

    Returns ``(budget, None)`` when ``budget > 0``; else ``(0.0, reason)``.
    """
    if equity <= 0:
        return 0.0, "equity <= 0"
    max_total_frac, max_single_frac = _options_pct_limits(config)
    cap_total = float(equity) * max_total_frac
    cap_single = float(equity) * max_single_frac
    risk_frac = max_premium_frac_of_equity(config)
    risk_dollars = float(equity) * risk_frac
    total_existing = sum_open_option_positions_premium(positions)
    room_total = max(0.0, cap_total - total_existing)
    max_premium_budget = min(risk_dollars, cap_single, room_total)
    per_trade_usd = max_premium_per_trade_usd(config)
    if per_trade_usd is not None:
        max_premium_budget = min(max_premium_budget, float(per_trade_usd))
    if max_premium_budget <= 0:
        return 0.0, (
            "options exposure cap (no premium budget left; existing $%.2f vs cap $%.2f)"
            % (total_existing, cap_total)
        )
    return float(max_premium_budget), None


@dataclass(frozen=True)
class OptionsPremiumGateResult:
    ok: bool
    reason: str | None
    contracts: int  # 0 if skip


@dataclass(frozen=True)
class OptionsOrderRiskControlResult:
    """Pre-order long-premium risk controls and sizing audit."""

    ok: bool
    reason: str | None
    contracts: int
    premium_budget_usd: float
    max_loss_usd: float
    daily_loss_limit_usd: float | None = None


def evaluate_options_premium_before_order(
    config: dict[str, Any],
    *,
    equity: float,
    positions: list[dict[str, Any]] | None,
    option_mid_price: float,
) -> OptionsPremiumGateResult:
    """
    contracts = floor(premium_budget / (mid × 100)) with
    ``premium_budget = min(equity × premium_frac, equity × max_option_position_pct, room under total cap, max_premium_per_trade?)``.
    """
    if equity <= 0:
        return OptionsPremiumGateResult(False, "equity <= 0", 0)
    if option_mid_price <= 0:
        return OptionsPremiumGateResult(False, "invalid option mid", 0)

    opts = config.get("options") or {}
    max_total_frac, _ = _options_pct_limits(config)
    cap_total = equity * max_total_frac
    mult = 100.0
    per_contract = option_mid_price * mult

    max_premium_budget, b_err = max_premium_budget_usd(
        config, equity=float(equity), positions=positions
    )
    if max_premium_budget <= 0:
        return OptionsPremiumGateResult(False, b_err or "no premium budget", 0)

    contracts = int(max_premium_budget // per_contract)
    contracts = max(0, contracts)
    if contracts < 1:
        return OptionsPremiumGateResult(
            False,
            "premium too expensive (budget $%.2f, 1 contract ≈ $%.2f at mid)"
            % (max_premium_budget, per_contract),
            0,
        )

    total_existing = sum_open_option_positions_premium(positions)
    raw_max = opts.get("v1_max_contracts_per_trade")
    if raw_max is None or str(raw_max).strip() == "":
        raw_max = opts.get("max_contracts_per_trade")
    if raw_max is None or str(raw_max).strip() == "":
        v1_max = 10**9
    else:
        try:
            v1_max = max(1, int(raw_max))
        except (TypeError, ValueError):
            v1_max = 10**9
    contracts = min(contracts, v1_max)

    new_premium = contracts * per_contract
    if total_existing + new_premium > cap_total + 1e-6:
        return OptionsPremiumGateResult(
            False,
            "options exposure cap (existing $%.2f + new $%.2f > $%.2f)"
            % (total_existing, new_premium, cap_total),
            0,
        )

    return OptionsPremiumGateResult(True, None, contracts)


def _daily_loss_limit_usd(config: Mapping[str, Any] | None, equity: float) -> float | None:
    opts = (config or {}).get("options") if isinstance(config, Mapping) else {}
    if not isinstance(opts, Mapping):
        return None
    raw = opts.get("max_daily_option_loss_pct")
    if raw is None or str(raw).strip() == "":
        raw = opts.get("max_daily_loss_pct")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        pct = abs(float(raw))
    except (TypeError, ValueError):
        return None
    if pct <= 0 or equity <= 0:
        return None
    return float(equity) * (pct / 100.0)


def evaluate_options_order_risk_controls(
    config: dict[str, Any],
    *,
    equity: float,
    positions: list[dict[str, Any]] | None,
    option_mid_price: float,
    daily_options_realized_pl: float | None = None,
    daily_options_unrealized_pl: float | None = None,
) -> OptionsOrderRiskControlResult:
    """
    Evaluate all pre-order long-premium controls that keep one option trade bounded.

    Long premium max loss is the debit paid: ``contracts × mid × 100``. The contract count
    comes from :func:`evaluate_options_premium_before_order`, which already applies per-trade
    premium, single-position, aggregate exposure, and contract-count caps.
    """
    try:
        eq = float(equity)
    except (TypeError, ValueError):
        eq = 0.0
    daily_limit = _daily_loss_limit_usd(config, eq)
    daily_pl = 0.0
    supplied_daily = False
    for raw in (daily_options_realized_pl, daily_options_unrealized_pl):
        if raw is None or str(raw).strip() == "":
            continue
        supplied_daily = True
        try:
            daily_pl += float(raw)
        except (TypeError, ValueError):
            continue
    if supplied_daily and daily_limit is not None and daily_pl <= -daily_limit + 1e-9:
        return OptionsOrderRiskControlResult(
            ok=False,
            reason="daily options loss %.2f <= -%.2f" % (daily_pl, daily_limit),
            contracts=0,
            premium_budget_usd=0.0,
            max_loss_usd=0.0,
            daily_loss_limit_usd=daily_limit,
        )

    budget, _budget_err = max_premium_budget_usd(config, equity=eq, positions=positions)
    gate = evaluate_options_premium_before_order(
        config,
        equity=eq,
        positions=positions,
        option_mid_price=float(option_mid_price),
    )
    if not gate.ok:
        return OptionsOrderRiskControlResult(
            ok=False,
            reason=gate.reason,
            contracts=0,
            premium_budget_usd=max(0.0, float(budget)),
            max_loss_usd=0.0,
            daily_loss_limit_usd=daily_limit,
        )
    max_loss = float(gate.contracts) * float(option_mid_price) * 100.0
    return OptionsOrderRiskControlResult(
        ok=True,
        reason=None,
        contracts=int(gate.contracts),
        premium_budget_usd=max(0.0, float(budget)),
        max_loss_usd=max_loss,
        daily_loss_limit_usd=daily_limit,
    )
