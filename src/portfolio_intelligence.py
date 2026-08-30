"""
Portfolio-level gates: correlation buckets, overlap control, and beta-weighted exposure.

Prevents many simultaneous positions that behave as one book (e.g. NVDA + AMD + SMH + SOXL).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from src.capital_allocator_loop import _normalized_correlation_groups

log = logging.getLogger(__name__)


def _lookup_symbol_beta(sym_u: str, beta_map: Mapping[str, Any], default_beta: float) -> float:
    for key in (sym_u, sym_u.upper(), sym_u.lower()):
        if key in beta_map and str(key).strip() != "_default":
            try:
                return float(beta_map[key])
            except (TypeError, ValueError):
                pass
    for k, v in beta_map.items():
        if str(k).strip().upper() == sym_u and str(k).strip() != "_default":
            try:
                return float(v)
            except (TypeError, ValueError):
                break
    return default_beta


def merged_correlation_groups(config: dict[str, Any] | None) -> dict[str, frozenset[str]]:
    """
    Merge correlation group maps with precedence (later overrides earlier keys):

    ``correlation_groups`` (root) → ``portfolio.capital_allocator.correlation_groups``
    → ``portfolio_intelligence.correlation_groups``.
    """
    cfg = config or {}
    combined: dict[str, Any] = {}
    root = cfg.get("correlation_groups")
    if isinstance(root, dict):
        combined.update(root)
    port = cfg.get("portfolio") or {}
    ca = port.get("capital_allocator") or {}
    cg_ca = ca.get("correlation_groups")
    if isinstance(cg_ca, dict):
        combined.update(cg_ca)
    pi = cfg.get("portfolio_intelligence") or {}
    cg_pi = pi.get("correlation_groups")
    if isinstance(cg_pi, dict):
        combined.update(cg_pi)
    return _normalized_correlation_groups(combined)


def correlation_resolution_order(
    config: dict[str, Any] | None,
    merged: Mapping[str, frozenset[str]],
) -> list[str]:
    """
    Iteration order for resolving a symbol to **one** group when lists overlap.

    Groups named in ``portfolio_intelligence.correlation_group_priority`` are checked first;
    remaining merged keys follow in insertion order.
    """
    cfg = config or {}
    pi = cfg.get("portfolio_intelligence") or {}
    pri = pi.get("correlation_group_priority")
    seen: set[str] = set()
    order: list[str] = []
    if isinstance(pri, list):
        for g in pri:
            gk = str(g).strip().lower()
            if gk in merged and gk not in seen:
                order.append(gk)
                seen.add(gk)
    for k in merged:
        if k not in seen:
            order.append(k)
            seen.add(k)
    return order


def resolve_correlation_group(
    symbol: str,
    groups: Mapping[str, frozenset[str]],
    order: list[str],
) -> str | None:
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return None
    for g in order:
        if sym_u in groups.get(g, frozenset()):
            return g
    return None

def _symbols_held_long(positions):
    held = {}

    if not positions:
        return held

    for p in positions:
        if isinstance(p, str):
            sym = p.strip().upper()
            if sym:
                held[sym] = 0.0
            continue

        if not isinstance(p, dict):
            continue

        sym = str(p.get("symbol") or p.get("asset_symbol") or "").strip().upper()
        if not sym:
            continue

        side = str(p.get("side") or "long").strip().lower()
        if side == "short":
            continue

        mv_raw = p.get("market_value")
        if mv_raw is None:
            mv_raw = p.get("notional")
        if mv_raw is None:
            mv_raw = p.get("value")
        if mv_raw is None:
            mv_raw = 0

        try:
            mv = abs(float(mv_raw))
        except Exception:
            mv = 0.0

        held[sym] = mv

    return held

def count_distinct_symbols_in_group(
    group_name: str,
    held: Mapping[str, float],
    groups: Mapping[str, frozenset[str]],
    order: list[str],
) -> int:
    n = 0
    for sym in held:
        if resolve_correlation_group(sym, groups, order) == group_name:
            n += 1
    return n


def beta_units_for_group(
    group_name: str,
    held: Mapping[str, float],
    *,
    equity: float,
    groups: Mapping[str, frozenset[str]],
    order: list[str],
    beta_by_symbol: Mapping[str, Any],
    default_beta: float = 1.0,
) -> float:
    """Sum_i (MV_i / equity) * beta_i for positions whose resolved group is *group_name*."""
    if equity <= 1e-9:
        return 0.0
    total = 0.0
    for sym, mv in held.items():
        if mv <= 0:
            continue
        if resolve_correlation_group(sym, groups, order) != group_name:
            continue
        b = _lookup_symbol_beta(sym, beta_by_symbol, default_beta)
        total += (mv / equity) * b
    return total


def portfolio_intelligence_blocks_entry(
    symbol: str,
    *,
    positions: list[dict[str, Any]],
    account_equity: float,
    proposed_notional: float,
    config: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """
    Return (True, reason) when a **new** entry should be skipped for correlation / beta limits.

    *proposed_notional* — conservative estimate of the upcoming buy clip (used for beta ceiling).
    """
    cfg = config or {}
    pi = cfg.get("portfolio_intelligence") or {}
    if not bool(pi.get("enabled", False)):
        return False, None

    groups = merged_correlation_groups(cfg)
    order = correlation_resolution_order(cfg, groups)
    sym_u = str(symbol or "").strip().upper()
    if not sym_u:
        return False, None

    gname = resolve_correlation_group(sym_u, groups, order)
    if gname is None:
        return False, None

    held = _symbols_held_long(positions)
    held_syms = frozenset(held.keys())

    max_pos_raw = pi.get("max_positions_per_correlation_group")
    if max_pos_raw is None or str(max_pos_raw).strip() == "":
        max_pos_raw = (cfg.get("correlation") or {}).get("max_per_group")
    try:
        max_pos = int(float(max_pos_raw))
    except (TypeError, ValueError):
        max_pos = 0
    # YAML ``0`` means "inherit correlation.max_per_group" (see default.yaml).
    if max_pos == 0:
        try:
            max_pos = int(
                float((cfg.get("correlation") or {}).get("max_per_group", 0) or 0)
            )
        except (TypeError, ValueError):
            max_pos = 0
    max_pos = max(0, max_pos)

    if max_pos > 0:
        n_in_group = count_distinct_symbols_in_group(gname, held, groups, order)
        if sym_u not in held_syms:
            if n_in_group >= max_pos:
                return True, (
                    "portfolio_intelligence: correlation group %r already has %d symbol(s) "
                    "(max %d) — avoid stacking correlated names"
                    % (gname, n_in_group, max_pos)
                )

    max_beta_raw = pi.get("max_beta_units_per_group")
    try:
        max_beta = (
            float(max_beta_raw)
            if max_beta_raw is not None and str(max_beta_raw).strip() != ""
            else 0.0
        )
    except (TypeError, ValueError):
        max_beta = 0.0
    if max_beta > 1e-12 and account_equity > 1e-9:
        beta_map = pi.get("symbol_beta") if isinstance(pi.get("symbol_beta"), dict) else {}
        try:
            d_beta = float(beta_map.get("_default", 1.0))
        except (TypeError, ValueError):
            d_beta = 1.0
        b_new = _lookup_symbol_beta(sym_u, beta_map, d_beta)

        units_now = beta_units_for_group(
            gname,
            held,
            equity=float(account_equity),
            groups=groups,
            order=order,
            beta_by_symbol=beta_map,
            default_beta=d_beta,
        )
        marginal = (max(0.0, float(proposed_notional)) / float(account_equity)) * b_new
        if units_now + marginal > max_beta + 1e-9:
            return True, (
                "portfolio_intelligence: group %r beta-units %.3f + marginal %.3f > cap %.3f"
                % (gname, units_now, marginal, max_beta)
            )

    return False, None
