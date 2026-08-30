"""
Optional top-level ``sector:`` app config: default GICS/label bucket and sector-cap policy for
symbols missing from ``symbol_sector`` maps.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "parse_sector_config",
    "resolve_sector_key_for_sizing",
]


def parse_sector_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """
    - *default_sector* — bucket for positions not listed in the merged ``symbol_sector`` map
      (``compute_exposures``; legacy default ``unknown`` when config omitted).
    - *enforce_caps_on_unknown* — when false, :class:`PositionSizer` does not apply the per-sector
      share cap for symbols that were not explicitly listed in the map passed to ``size_position``.
    """
    raw = (config or {}).get("sector")
    if not isinstance(raw, dict):
        raw = {}
    ds = raw.get("default_sector", "unknown")
    default_sector = str(ds).strip() if ds is not None and str(ds).strip() else "unknown"
    return {
        "default_sector": default_sector,
        "enforce_caps_on_unknown": bool(raw.get("enforce_caps_on_unknown", True)),
    }


def resolve_sector_key_for_sizing(
    symbol: str,
    symbol_sector: dict[str, str] | None,
    *,
    default_sector: str,
) -> tuple[str, bool]:
    """
    Return ``(sector_key, explicit)`` where *explicit* is true when *symbol* had a non-empty
    value in *symbol_sector*.
    """
    su = str(symbol or "").strip().upper()
    if not su:
        return (default_sector, False)
    raw = (symbol_sector or {}).get(su)
    if raw is not None and str(raw).strip() != "":
        return (str(raw).strip(), True)
    return (default_sector, False)
