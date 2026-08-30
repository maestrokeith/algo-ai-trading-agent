"""
Long-option exit rules driven by ``options.exits`` in config.

Gated by ``options.exits.automation_enabled`` (default true), not ``options.new_entries_enabled``.

Uses Alpaca-style position economics: P/L %% ≈ ``unrealized_pl / abs(cost_basis) * 100``
for long premium. Underlying "signal break" uses daily close vs a simple MA on the
underlying (put → exit if close recovers above MA; call → exit if close falls below MA).

**Tiered profit take** (when ``profit_tier_partial_pct`` is set): e.g. ``if pnl >= 30: sell 50%``
of contracts — at ``profit_tier_partial_pct`` sell ``floor(open_qty * profit_tier_partial_fraction)``
contracts (capped so at least one remains unless qty is 1, then tier1 is skipped until final tier).
Tracker ``option_profit_tier1_done`` prevents repeating the partial. At ``profit_tier_final_pct``
sell the rest. Legacy ``profit_take_pct`` applies only when ``profit_tier_partial_pct`` is omitted.

**P/L trail** (when ``profit_trail_arm_peak_pct`` and ``profit_trail_exit_below_pct`` are set):
tracker ``option_pnl_peak_pct`` is ``peak_pnl``; ``current_pnl`` is live P/L %%. Rule:
``if peak_pnl >= arm and current_pnl < exit_below:`` full sell (see ``ExitReason.OPTION_PNL_TRAIL``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .options_selector import parse_occ_equity_option_symbol
from .strategy import ExitReason


def compute_option_pnl_pct(
    *,
    unrealized_pl: float | None,
    cost_basis: float,
    market_value: float | None = None,
) -> float | None:
    """
    Return unrealized P/L as %% of premium (cost basis) for a long option; None if unknown.

    Alpaca often reports long-option ``cost_basis`` as negative (cash out); when
    ``unrealized_pl`` is missing, derive it from ``market_value`` + cost_basis (negative
    basis) or ``market_value`` - abs(basis) (positive basis).
    """
    cb_raw = float(cost_basis or 0.0)
    cb = abs(cb_raw)
    if cb < 1e-6:
        return None

    upl = unrealized_pl
    if upl is None and market_value is not None:
        mv = float(market_value)
        if cb_raw < 0:
            upl = mv + cb_raw
        else:
            upl = mv - cb_raw

    if upl is None:
        return None
    return float(upl) / cb * 100.0


def underlying_breaks_option_signal(right: str, underlying_close: float, ma: float) -> bool:
    """
    Long put (bearish): thesis breaks if underlying closes *above* MA (recovery).
    Long call (bullish): thesis breaks if underlying closes *below* MA.
    """
    r = str(right or "").strip().lower()
    if r in ("put", "puts"):
        return float(underlying_close) > float(ma)
    if r in ("call", "calls"):
        return float(underlying_close) < float(ma)
    return False


def _contracts_for_tiered_partial(open_qty: int, fraction: float) -> int | None:
    """Contracts to sell at first profit tier; None if partial is not meaningful."""
    if open_qty <= 0 or fraction <= 0:
        return None
    if open_qty == 1:
        return None
    raw = int(math.floor(open_qty * float(fraction)))
    if raw < 1:
        raw = 1
    return min(raw, open_qty - 1)


@dataclass(frozen=True)
class OptionExitDecision:
    should_exit: bool
    reason: ExitReason | None
    message: str
    pnl_pct: float | None
    contracts_to_sell: int | None = None
    remove_tracker_after: bool = True
    mark_option_profit_tier1_done: bool = False
    persist_option_pnl_peak_pct: float | None = None


def evaluate_long_option_exit(
    config: dict[str, Any],
    *,
    occ_symbol: str,
    days_held: int,
    unrealized_pl: float | None,
    cost_basis: float,
    underlying_close: float | None,
    underlying_ma: float | None,
    market_value: float | None = None,
    open_contracts: int | None = None,
    tracker_position: dict[str, Any] | None = None,
) -> OptionExitDecision:
    """
    Apply ``options.exits`` thresholds. Evaluation order:

    1. Stop-loss on P/L %%: ``if pnl <= -stop_loss_pct:`` full sell (``OPTION_STOP_LOSS``)
    2. Underlying signal break (if enabled and MA available)
    3. P/L trail (peak vs arm / exit-below) when configured
    4. Tiered profit take (if ``profit_tier_partial_pct`` set) else single profit_take_pct
    5. Max calendar days held (``days_held`` from tracker / same basis as ``bars_held``)
    """
    opts = config.get("options") or {}
    ex = opts.get("exits") or {}
    if not bool(ex.get("automation_enabled", True)):
        return OptionExitDecision(False, None, "options.exits.automation_enabled is false", None)

    parsed = parse_occ_equity_option_symbol(str(occ_symbol).strip().upper())
    if parsed is None:
        return OptionExitDecision(False, None, "unparseable OCC symbol", None)
    _root, _exp, right, _strike = parsed
    stop_pct = float(ex.get("stop_loss_pct", 20) or 0)
    profit_pct = float(ex.get("profit_take_pct", 40) or 0)
    max_days = int(ex.get("max_hold_days", 5) or 0)
    check_break = bool(ex.get("exit_if_underlying_breaks_signal", False))

    pnl_pct = compute_option_pnl_pct(
        unrealized_pl=unrealized_pl,
        cost_basis=cost_basis,
        market_value=market_value,
    )

    if pnl_pct is not None and stop_pct > 0 and pnl_pct <= -stop_pct:
        return OptionExitDecision(
            True,
            ExitReason.OPTION_STOP_LOSS,
            "option P/L %.1f%% <= -%.0f%% stop" % (pnl_pct, stop_pct),
            pnl_pct,
        )

    if (
        check_break
        and underlying_close is not None
        and underlying_ma is not None
        and float(underlying_ma) > 0
    ):
        if underlying_breaks_option_signal(right, float(underlying_close), float(underlying_ma)):
            return OptionExitDecision(
                True,
                ExitReason.OPTION_UNDERLYING_BREAK,
                "underlying close %.4g vs MA %.4g — %s signal break"
                % (float(underlying_close), float(underlying_ma), right),
                pnl_pct,
            )

    persist_peak: float | None = None
    _ta = ex.get("profit_trail_arm_peak_pct")
    _tb = ex.get("profit_trail_exit_below_pct")
    trail_on = (
        pnl_pct is not None
        and _ta is not None
        and str(_ta).strip() != ""
        and _tb is not None
        and str(_tb).strip() != ""
    )
    trail_arm = 40.0
    trail_below = 25.0
    if trail_on:
        try:
            trail_arm = float(_ta)
            trail_below = float(_tb)
        except (TypeError, ValueError):
            trail_on = False
    if trail_on and pnl_pct is not None:
        prev_raw = (tracker_position or {}).get("option_pnl_peak_pct")
        prev_peak: float | None = None
        try:
            if prev_raw is not None and str(prev_raw).strip() != "":
                prev_peak = float(prev_raw)
        except (TypeError, ValueError):
            prev_peak = None
        peak_pnl = max(prev_peak if prev_peak is not None else pnl_pct, pnl_pct)
        persist_peak = peak_pnl
        current_pnl = pnl_pct
        if peak_pnl >= trail_arm and current_pnl < trail_below:
            return OptionExitDecision(
                True,
                ExitReason.OPTION_PNL_TRAIL,
                "option P/L trail: peak %.1f%% >= +%.0f%% and P/L %.1f%% < +%.0f%%"
                % (peak_pnl, trail_arm, current_pnl, trail_below),
                pnl_pct,
            )

    tier_partial_raw = ex.get("profit_tier_partial_pct")
    use_tiered = tier_partial_raw is not None and str(tier_partial_raw).strip() != ""
    if pnl_pct is not None and use_tiered:
        try:
            t1 = float(tier_partial_raw)
        except (TypeError, ValueError):
            t1 = 30.0
        t2_raw = ex.get("profit_tier_final_pct")
        try:
            t2 = float(t2_raw) if t2_raw is not None and str(t2_raw).strip() != "" else 50.0
        except (TypeError, ValueError):
            t2 = 50.0
        try:
            f1 = float(ex.get("profit_tier_partial_fraction", 0.5) or 0.5)
        except (TypeError, ValueError):
            f1 = 0.5
        f1 = max(0.01, min(0.99, f1))
        tier1_done = bool((tracker_position or {}).get("option_profit_tier1_done"))
        open_q = int(open_contracts or 0)

        if pnl_pct >= t2:
            return OptionExitDecision(
                True,
                ExitReason.OPTION_PROFIT_TAKE,
                "option P/L %.1f%% >= +%.0f%% — sell remaining" % (pnl_pct, t2),
                pnl_pct,
                None,
                True,
                False,
                None,
            )
        # Tier 1: if pnl >= t1: sell fraction of qty (e.g. pnl >= 30 → sell 50%).
        if pnl_pct >= t1 and not tier1_done:
            sell_n = _contracts_for_tiered_partial(open_q, f1)
            if sell_n is not None and sell_n > 0:
                return OptionExitDecision(
                    True,
                    ExitReason.OPTION_PROFIT_TAKE_PARTIAL,
                    "option P/L %.1f%% >= +%.0f%% — sell %d of %d contracts (%.0f%%)"
                    % (pnl_pct, t1, sell_n, open_q, f1 * 100.0),
                    pnl_pct,
                    sell_n,
                    False,
                    True,
                    persist_peak,
                )
    elif pnl_pct is not None and profit_pct > 0 and pnl_pct >= profit_pct:
        return OptionExitDecision(
            True,
            ExitReason.OPTION_PROFIT_TAKE,
            "option P/L %.1f%% >= +%.0f%% target" % (pnl_pct, profit_pct),
            pnl_pct,
        )

    if max_days > 0 and int(days_held) >= max_days:
        return OptionExitDecision(
            True,
            ExitReason.OPTION_MAX_HOLD,
            "held %d calendar days >= max_hold_days %d" % (int(days_held), max_days),
            pnl_pct,
        )

    return OptionExitDecision(False, None, "", pnl_pct, persist_option_pnl_peak_pct=persist_peak)
