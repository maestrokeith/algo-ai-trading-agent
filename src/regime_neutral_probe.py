"""Optional floor for trend-long sizing when market regime is neutral (live loop)."""
from __future__ import annotations

from typing import Any


def apply_neutral_probe_size_floor(
    trend_long_regime_mult: float,
    *,
    regime_condition: str | None,
    probe_cfg: dict[str, Any] | None,
) -> tuple[float, bool]:
    """
    If ``regime.neutral_probe`` is enabled and condition is ``neutral``, ensure
    ``trend_long_regime_mult`` is at least ``size_multiplier``.

    Returns ``(adjusted_mult, True)`` if a floor was applied; else ``(mult, False)``.
    """
    p = probe_cfg or {}
    if not bool(p.get("enabled", False)):
        return trend_long_regime_mult, False
    if regime_condition != "neutral":
        return trend_long_regime_mult, False
    floor = float(p.get("size_multiplier", 0.3))
    if floor <= 0:
        return trend_long_regime_mult, False
    if trend_long_regime_mult >= floor:
        return trend_long_regime_mult, False
    return floor, True
