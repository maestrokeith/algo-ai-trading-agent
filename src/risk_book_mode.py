"""
Presets for deployable gross vs cash reserve ("risk book mode").

Read ``portfolio.risk_book_mode`` (``safe`` | ``balanced`` | ``aggressive``).
Use ``custom`` or omit / empty to leave YAML values unchanged.

The live engine caps effective gross exposure at 100%% of equity; there is no code path for 105%%
gross without margin/leverage changes — aggressive mode uses 100%% cap + minimal cash reserve (%% of equity).
"""
from __future__ import annotations

from typing import Any

_MODE_PRESETS: dict[str, dict[str, Any]] = {
    # Max gross ≈ portfolio.exposure_gates cap; reserve = min cash held back vs equity for stock BP.
    "safe": {
        "max_gross_frac": 0.85,
        "min_cash_reserve_pct": 15,
        "adaptive_regime": {"bullish": 0.85, "neutral": 0.85, "bearish": 0.85},
        "cash_reserve_by_regime": {"bullish": 15, "neutral": 15, "bearish": 15},
        "boost_signals": False,
    },
    "balanced": {
        "max_gross_frac": 0.95,
        "min_cash_reserve_pct": 5,
        "adaptive_regime": {"bullish": 0.95, "neutral": 0.95, "bearish": 0.95},
        "cash_reserve_by_regime": {"bullish": 5, "neutral": 5, "bearish": 5},
        "boost_signals": True,
    },
    "aggressive": {
        "max_gross_frac": 1.0,
        "min_cash_reserve_pct": 1,
        "adaptive_regime": {"bullish": 1.0, "neutral": 1.0, "bearish": 0.98},
        "cash_reserve_by_regime": {"bullish": 1, "neutral": 1, "bearish": 2},
        "boost_signals": True,
    },
}


def apply_risk_book_mode(config: dict[str, Any] | None) -> None:
    """
    Mutate *config* in place when ``portfolio.risk_book_mode`` is a known preset.

    Sets ``portfolio.exposure_gates.max_total_exposure_frac``,
    ``portfolio.target_gross_exposure_pct``, ``portfolio.min_cash_reserve_pct``,
    ``adaptive.max_exposure_by_regime``, ``cash_management.reserve_by_regime``,
    and optionally ``adaptive.boost_exposure_if_many_signals``.
    """
    if not isinstance(config, dict):
        return
    port = config.get("portfolio")
    raw = None
    if isinstance(port, dict):
        raw = port.get("risk_book_mode")
    if raw is None:
        raw = config.get("risk_book_mode")
    if raw is None or str(raw).strip() == "":
        return
    key = str(raw).strip().lower()
    if key in ("custom", "none", "off", "false"):
        return
    preset = _MODE_PRESETS.get(key)
    if preset is None:
        return

    g = float(preset["max_gross_frac"])
    r_pct = float(preset["min_cash_reserve_pct"])

    port_m = config.setdefault("portfolio", {})
    eg = port_m.setdefault("exposure_gates", {})
    eg["max_total_exposure_frac"] = g
    port_m["target_gross_exposure_pct"] = g
    port_m["min_cash_reserve_pct"] = r_pct

    ad = config.setdefault("adaptive", {})
    ad["max_exposure_by_regime"] = dict(preset["adaptive_regime"])
    ad["boost_exposure_if_many_signals"] = bool(preset["boost_signals"])

    cm = config.setdefault("cash_management", {})
    cm["reserve_by_regime"] = dict(preset["cash_reserve_by_regime"])

    # Align score-3 floor with neutral cap so mode isn't contradicted by regime_3.max_exposure.
    r3 = ad.get("regime_3")
    if isinstance(r3, dict):
        r3 = dict(r3)
        r3["max_exposure"] = float(preset["adaptive_regime"].get("neutral", g))
        ad["regime_3"] = r3


def list_risk_book_modes() -> frozenset[str]:
    return frozenset(_MODE_PRESETS.keys())
