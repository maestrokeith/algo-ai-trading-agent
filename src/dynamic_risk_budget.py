"""
``portfolio.allocator.mode: dynamic_risk_budget`` — soft bucket targets and cadence
instead of rigid ``max_stock_capital_pct`` / cash reserve / inverse-ETF caps.

Bucket ``target`` values are percent of total equity (0–100) for **notional policy**;
the engine normalizes to fractions and exposes:

* :func:`effective_drb_cash_buffer_frac` — cash held back from deployable buying power
  (replaces a fixed ``min_cash_reserve_pct`` when the mode is on).
* :func:`hedge_bucket_target_pct` — maps the ``hedge`` bucket to
  ``position_sizing.max_inverse_etf_exposure_pct`` (inverse book vs equity).
* :func:`rebalance_interval_sec` — parse ``rebalance_frequency`` (``15m``, ``1h``).

**Stock vs options split:** :func:`portfolio_allocation.effective_stock_capital_frac` uses
``1 − options_sleeve_frac`` when this mode is active (no fixed 60% stock cap).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

MODE_DYNAMIC_RISK_BUDGET = "dynamic_risk_budget"


@dataclass(frozen=True)
class DynamicRiskBudgetConfig:
    """Parsed ``portfolio.allocator`` when *mode* is :data:`MODE_DYNAMIC_RISK_BUDGET`."""

    bucket_targets_pp: dict[str, float]  # percent of equity, keys lowercased
    rebalance_interval_sec: int
    cash_buffer_pp: float  # percent of equity, deployable-BP hair cut (0–100)
    hedge_target_pp: float | None  # from ``hedge`` bucket; drives inverse-ETF cap

    @property
    def bucket_fracs(self) -> dict[str, float]:
        s = sum(max(0.0, float(x)) for x in self.bucket_targets_pp.values())
        if s <= 1e-12:
            return {}
        return {k: max(0.0, float(v)) / s for k, v in self.bucket_targets_pp.items()}


def _parse_one_bucket_target(raw: Any) -> float:
    if raw is None or str(raw).strip() == "":
        return 0.0
    if isinstance(raw, Mapping):
        t = raw.get("target", raw.get("pct"))
    else:
        t = raw
    try:
        v = float(t)
    except (TypeError, ValueError):
        return 0.0
    s = str(t) if t is not None else ""
    if s.endswith("%"):
        try:
            v = float(s[:-1].strip())
        except (TypeError, ValueError):
            return 0.0
    return max(0.0, min(100.0, v))


def _parse_bucket_map(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        key = str(k or "").strip().lower()
        if not key:
            continue
        out[key] = _parse_one_bucket_target(v)
    return out


def rebalance_interval_sec(raw: Any) -> int:
    """
    ``15m`` / ``15M`` / ``900`` / ``1h`` / ``30s`` → seconds. ``0`` or invalid → ``0`` (off).
    """
    if raw is None or str(raw).strip() == "":
        return 0
    s = str(raw).strip().lower()
    if s.isdigit() or (s.replace(".", "", 1).isdigit() and s.count(".") <= 1):
        try:
            return max(0, int(float(s)))
        except (TypeError, ValueError):
            return 0
    m = re.match(
        r"^\s*(\d+(?:\.\d+)?)\s*([smhd])?\s*$",
        s,
    )
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except (TypeError, ValueError):
        return 0
    unit = (m.group(2) or "s").lower()
    if unit == "s":
        return max(0, int(round(n)))
    if unit == "m":
        return max(0, int(round(n * 60.0)))
    if unit == "h":
        return max(0, int(round(n * 3600.0)))
    if unit == "d":
        return max(0, int(round(n * 86400.0)))
    return 0


def parse_dynamic_risk_budget(config: dict[str, Any] | None) -> DynamicRiskBudgetConfig | None:
    """
    Return a frozen config when ``portfolio.allocator.mode == dynamic_risk_budget``, else ``None``.

    ``cash_buffer_pct`` (optional) — percent of equity reserved (same style as
    ``portfolio.min_cash_reserve_pct``) when the mode is on, replacing the base reserve.
    """
    if not config or not isinstance(config, dict):
        return None
    port = config.get("portfolio") or {}
    if not isinstance(port, dict):
        return None
    alloc = port.get("allocator")
    if not isinstance(alloc, dict):
        return None
    mode = str(alloc.get("mode", "")).strip().lower()
    if mode != MODE_DYNAMIC_RISK_BUDGET:
        return None
    buckets = _parse_bucket_map(alloc.get("buckets"))
    ival = rebalance_interval_sec(alloc.get("rebalance_frequency"))
    cb_raw = alloc.get("cash_buffer_pct")
    try:
        cash_buffer = float(cb_raw) if cb_raw is not None and str(cb_raw).strip() != "" else 0.0
    except (TypeError, ValueError):
        cash_buffer = 0.0
    if cash_buffer > 0 and cash_buffer <= 1.0:
        cash_buffer *= 100.0
    cash_buffer = max(0.0, min(100.0, cash_buffer))
    ht = buckets.get("hedge")
    hedge_t = ht if ht > 0 else None
    return DynamicRiskBudgetConfig(
        bucket_targets_pp=buckets,
        rebalance_interval_sec=ival,
        cash_buffer_pp=cash_buffer,
        hedge_target_pp=hedge_t,
    )


def dynamic_risk_budget_enabled(config: dict[str, Any] | None) -> bool:
    return parse_dynamic_risk_budget(config) is not None


def effective_drb_cash_buffer_frac(config: dict[str, Any] | None) -> float:
    """``[0,1]`` cash buffer when DRB is active; ``0`` if not active."""
    c = parse_dynamic_risk_budget(config)
    if c is None:
        return 0.0
    return max(0.0, min(1.0, c.cash_buffer_pp / 100.0))


def hedge_bucket_target_pct(config: dict[str, Any] | None) -> float | None:
    """
    Percent of equity (0–100) for the **hedge** bucket (drives inverse-ETF exposure cap);
    ``None`` when the mode is off or ``hedge`` is unset.
    """
    c = parse_dynamic_risk_budget(config)
    if c is None or c.hedge_target_pp is None:
        return None
    return float(c.hedge_target_pp)


def rebalance_due(
    now_ts: float,
    last_rebalance_ts: float | None,
    interval_sec: int,
) -> bool:
    """``True`` when the clock says a *bucket rebalance* tick may run."""
    if interval_sec <= 0:
        return False
    if last_rebalance_ts is None:
        return True
    return (now_ts - float(last_rebalance_ts)) + 1e-6 >= float(interval_sec)
