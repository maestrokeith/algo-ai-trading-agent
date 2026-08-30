"""
Phase-1 **Portfolio Brain**: portfolio-level gates before per-symbol execution.

Inputs (wired gradually): open positions, optional sector exposure map, optional
symbol→sector map, optional cash/regime hints. Outputs a single structured decision
for logging and routing.

Later phases can add correlation buckets, regime-conditioned caps, and deployable-cash
floors without changing the public decision shape.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from src.portfolio_allocation import symbol_long_position_market_value_usd
from src.execution import execution_bucket_top_signal_qualified
from src.risk_limits import (
    allow_cross_bucket_rebalance,
    effective_max_sector_sleeve_pct,
    effective_symbol_allocation_cap_pct,
    other_sleeves_dollar_headroom,
    risk_bucket_key_for_symbol,
    risk_effective_max_bucket_allocation_frac_for_bucket,
    risk_max_new_positions_per_cycle,
    sum_long_stock_mv_in_bucket,
)

logger = logging.getLogger(__name__)


class PortfolioBrainDecision(TypedDict):
    """JSON-serializable portfolio gate result for one candidate symbol."""

    allow_new_positions: bool
    max_new_trades: int
    symbol_allowed: bool
    reason: str


def portfolio_brain_enabled(config: dict[str, Any] | None) -> bool:
    pb = ((config or {}).get("portfolio") or {}).get("portfolio_brain") or {}
    return bool(pb.get("enabled", False))


def _high_cash_cap_multipliers(
    config: dict[str, Any] | None, *, high_cash_deploy: bool
) -> tuple[float, float, float]:
    """Return (bucket_cap_mult, symbol_cap_mult, sector_exposure_mult), each ≥ 1 when high cash."""
    if not high_cash_deploy:
        return (1.0, 1.0, 1.0)
    pb = ((config or {}).get("portfolio") or {}).get("portfolio_brain") or {}
    wh = pb.get("when_high_cash") if isinstance(pb.get("when_high_cash"), dict) else {}

    def _one(key: str) -> float:
        try:
            v = float(wh.get(key, 1.0))
        except (TypeError, ValueError):
            return 1.0
        return max(1.0, min(v, 3.0))

    return (
        _one("bucket_cap_mult"),
        _one("symbol_cap_mult"),
        _one("sector_exposure_mult"),
    )


def bucket_exposure_frac(
    bucket_key: str,
    *,
    positions: list[dict[str, Any]] | None,
    equity: float,
    config: dict[str, Any] | None,
    sector_etfs: frozenset[str],
) -> float:
    """Long-stock market value in *bucket_key* / equity (0 if equity <= 0)."""
    eq = float(equity)
    if eq <= 0:
        return 0.0
    mv = sum_long_stock_mv_in_bucket(positions, bucket_key, config, sector_etfs)
    return mv / eq


def symbol_exposure_frac(
    symbol: str,
    *,
    positions: list[dict[str, Any]] | None,
    equity: float,
) -> float:
    """Current long equity ``market_value`` for *symbol* / equity (excludes options)."""
    eq = float(equity)
    if eq <= 0:
        return 0.0
    return symbol_long_position_market_value_usd(positions, str(symbol).strip().upper()) / eq


def _max_sector_exposure_pct(config: dict[str, Any] | None) -> float:
    return float(effective_max_sector_sleeve_pct(config))


def portfolio_brain(
    symbol: str,
    *,
    positions: list[dict[str, Any]] | None,
    equity: float,
    config: dict[str, Any] | None,
    sector_etfs: frozenset[str],
    cash: float | None = None,
    sector_exposure_pct: dict[str, float] | None = None,
    symbol_sector: dict[str, str] | None = None,
    regime_score: int | None = None,
    regime_condition: str | None = None,
    entry_strength: float | None = None,
    strength_cohort: list[float] | None = None,
    high_cash_deploy: bool = False,
    skip_symbol_allocation_cap_gate: bool = False,
) -> PortfolioBrainDecision:
    """
    Return concentration and (optional) sector exposure decision for *symbol*.

    ``regime_score`` / ``regime_condition`` (with ``adaptive.bucket_cap_multiplier``) scale the per-bucket
    equity ceiling. ``entry_strength`` + ``strength_cohort`` enable ``execution.allow_bucket_override_for_top_signals``
    to bypass a bucket **block**; ``cash`` is reserved for future hooks.

    When     ``high_cash_deploy`` is true and ``portfolio_brain.when_high_cash`` defines
    multipliers, bucket / symbol / sector *ceilings* are scaled up (looser gates).

    ``skip_symbol_allocation_cap_gate``: when true (e.g. pyramid-into-winners path in the live loop),
    the per-name concentration check is skipped; bucket / sector gates still apply.
    """
    _ = cash
    b_mult, sym_mult, sec_mult = _high_cash_cap_multipliers(config, high_cash_deploy=high_cash_deploy)
    sym_u = str(symbol or "").strip().upper()
    eq = float(equity)
    if eq <= 0:
        return PortfolioBrainDecision(
            allow_new_positions=False,
            max_new_trades=0,
            symbol_allowed=False,
            reason="invalid equity",
        )

    cap = risk_max_new_positions_per_cycle(config)
    max_new = int(cap) if cap > 0 else 999

    sym_u_for_bucket = sym_u
    bkey = risk_bucket_key_for_symbol(config, sym_u_for_bucket, sector_etfs)
    bucket_frac_cap = risk_effective_max_bucket_allocation_frac_for_bucket(
        config,
        bkey,
        regime_condition=regime_condition,
        regime_score=regime_score,
    )
    bucket_ceiling = min(1.0, bucket_frac_cap * b_mult) if bucket_frac_cap > 0 else 0.0
    if bucket_frac_cap > 0:
        be = bucket_exposure_frac(
            bkey, positions=positions, equity=eq, config=config, sector_etfs=sector_etfs
        )
        osl = 0.0
        if allow_cross_bucket_rebalance(config):
            osl = other_sleeves_dollar_headroom(
                config,
                positions,
                sector_etfs,
                eq,
                bkey,
                regime_condition=regime_condition,
                regime_score=regime_score,
                bucket_cap_mult=b_mult,
            )
        eff_ceil = min(1.0, bucket_ceiling + (osl / eq if eq > 0 else 0.0))
        if be >= eff_ceil - 1e-12:
            if entry_strength is not None and execution_bucket_top_signal_qualified(
                config,
                strength=entry_strength,
                strength_cohort=strength_cohort,
            ):
                pass
            else:
                _cap_lbl = eff_ceil * 100.0 if osl else bucket_ceiling * 100.0
                msg = "bucket limit hit (%s %.1f%% >= %.1f%%)" % (
                    bkey,
                    be * 100.0,
                    _cap_lbl,
                )
                logger.debug("portfolio_brain %s: %s", sym_u, msg)
                return PortfolioBrainDecision(
                    allow_new_positions=True,
                    max_new_trades=max_new,
                    symbol_allowed=False,
                    reason=msg,
                )

    sym_cap_pct = effective_symbol_allocation_cap_pct(
        config,
        account_equity=eq,
        symbol_upper=sym_u,
    )
    if sym_cap_pct > 0 and not skip_symbol_allocation_cap_gate:
        sym_frac = symbol_exposure_frac(sym_u, positions=positions, equity=eq)
        sym_cap_pct_eff = min(100.0, sym_cap_pct * sym_mult)
        sym_cap_frac = sym_cap_pct_eff / 100.0
        if sym_frac >= sym_cap_frac - 1e-12:
            msg = "symbol cap hit (%.1f%% >= %.1f%%)" % (sym_frac * 100.0, sym_cap_pct_eff)
            logger.debug("portfolio_brain %s: %s", sym_u, msg)
            return PortfolioBrainDecision(
                allow_new_positions=True,
                max_new_trades=max_new,
                symbol_allowed=False,
                reason=msg,
            )

    max_sec = _max_sector_exposure_pct(config)
    max_sec_eff = min(100.0, max_sec * sec_mult) if max_sec > 0 else 0.0
    if (
        max_sec > 0
        and sector_exposure_pct
        and symbol_sector
        and sym_u in symbol_sector
    ):
        sector = str(symbol_sector[sym_u] or "").strip() or "unknown"
        cur = float(sector_exposure_pct.get(sector, 0.0))
        if cur >= max_sec_eff - 1e-9:
            msg = "%s overexposed (%.1f%% >= %.1f%%)" % (sector, cur, max_sec_eff)
            logger.debug("portfolio_brain %s: %s", sym_u, msg)
            return PortfolioBrainDecision(
                allow_new_positions=True,
                max_new_trades=max_new,
                symbol_allowed=False,
                reason=msg,
            )

    return PortfolioBrainDecision(
        allow_new_positions=True,
        max_new_trades=max_new,
        symbol_allowed=True,
        reason="ok",
    )
