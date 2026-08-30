"""Sector and theme momentum helpers for dynamic symbol ranking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class ThemeDefinition:
    name: str
    etfs: tuple[str, ...]
    symbols: tuple[str, ...]


DEFAULT_THEMES: dict[str, ThemeDefinition] = {
    "ai": ThemeDefinition(
        "ai",
        ("AIQ", "BOTZ", "XLK"),
        ("NVDA", "PLTR", "MSFT", "GOOGL", "META", "AMZN", "ANET", "SMCI"),
    ),
    "semiconductors": ThemeDefinition(
        "semiconductors",
        ("SMH", "SOXX"),
        ("NVDA", "AMD", "AVGO", "MU", "ARM", "MRVL", "QCOM", "TSM", "INTC", "SMCI"),
    ),
    "cybersecurity": ThemeDefinition(
        "cybersecurity",
        ("CIBR", "HACK"),
        ("CRWD", "PANW", "ZS", "FTNT", "OKTA", "S", "NET"),
    ),
    "energy": ThemeDefinition(
        "energy",
        ("XLE", "OIH"),
        ("XOM", "CVX", "COP", "SLB", "HAL", "OXY", "MPC", "VLO"),
    ),
    "biotech": ThemeDefinition(
        "biotech",
        ("XBI", "IBB"),
        ("MRNA", "BIIB", "GILD", "REGN", "VRTX", "BMRN", "SRPT"),
    ),
}


def _tuple_symbols(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple, set)):
        return ()
    return tuple(
        dict.fromkeys(
            str(item or "").strip().upper()
            for item in raw
            if str(item or "").strip()
        )
    )


def load_theme_definitions(config: Mapping[str, Any] | None) -> dict[str, ThemeDefinition]:
    """Return built-in theme definitions merged with optional config overrides."""
    raw = config.get("theme_intelligence") if isinstance(config, Mapping) else None
    theme_cfg = raw if isinstance(raw, Mapping) else {}
    definitions = dict(DEFAULT_THEMES)
    overrides = theme_cfg.get("themes") if isinstance(theme_cfg, Mapping) else None
    if isinstance(overrides, Mapping):
        for name, row in overrides.items():
            if not isinstance(row, Mapping):
                continue
            key = str(name or "").strip().lower()
            if not key:
                continue
            base = definitions.get(key, ThemeDefinition(key, (), ()))
            definitions[key] = ThemeDefinition(
                key,
                _tuple_symbols(row.get("etfs")) or base.etfs,
                _tuple_symbols(row.get("symbols")) or base.symbols,
            )
    return definitions


def theme_intelligence_enabled(config: Mapping[str, Any] | None) -> bool:
    raw = config.get("theme_intelligence") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        return False
    return bool(raw.get("enabled", False))


def theme_bonus_weight(config: Mapping[str, Any] | None) -> float:
    raw = config.get("theme_intelligence") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        return 0.25
    try:
        return max(0.0, min(2.0, float(raw.get("bonus_weight", 0.25) or 0.25)))
    except (TypeError, ValueError):
        return 0.25


def theme_bonus_cap(config: Mapping[str, Any] | None) -> float:
    raw = config.get("theme_intelligence") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        return 3.0
    try:
        return max(0.0, min(10.0, float(raw.get("max_bonus", 3.0) or 3.0)))
    except (TypeError, ValueError):
        return 3.0


def theme_for_symbol(symbol: str, config: Mapping[str, Any] | None = None) -> str | None:
    """Return the configured theme bucket for *symbol*, if known."""
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    for name, definition in load_theme_definitions(config).items():
        if sym in definition.symbols or sym in definition.etfs:
            return name
    return None


def theme_etf_symbols(config: Mapping[str, Any] | None = None) -> list[str]:
    """All ETF symbols needed for theme momentum snapshots."""
    out: list[str] = []
    for definition in load_theme_definitions(config).values():
        for etf in definition.etfs:
            if etf not in out:
                out.append(etf)
    return out


def _snapshot_gain(snapshot: Mapping[str, Any] | None) -> float | None:
    if not isinstance(snapshot, Mapping):
        return None
    try:
        return float(snapshot.get("day_gain_pct") or 0.0)
    except (TypeError, ValueError):
        return None


def theme_momentum_scores(
    snapshots: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """Average ETF day-gain momentum by theme."""
    scores: dict[str, float] = {}
    for name, definition in load_theme_definitions(config).items():
        gains = [
            gain
            for gain in (_snapshot_gain(snapshots.get(etf)) for etf in definition.etfs)
            if gain is not None
        ]
        if gains:
            scores[name] = sum(gains) / len(gains)
    return scores


def symbol_theme_bonus(
    symbol: str,
    theme_scores: Mapping[str, float],
    config: Mapping[str, Any] | None = None,
) -> tuple[str | None, float]:
    """Return matching theme and positive momentum bonus for a symbol."""
    theme = theme_for_symbol(symbol, config)
    if not theme:
        return None, 0.0
    score = float(theme_scores.get(theme, 0.0) or 0.0)
    if score <= 0:
        return theme, 0.0
    bonus = min(theme_bonus_cap(config), score * theme_bonus_weight(config))
    return theme, float(bonus)


__all__ = [
    "DEFAULT_THEMES",
    "ThemeDefinition",
    "load_theme_definitions",
    "symbol_theme_bonus",
    "theme_bonus_cap",
    "theme_bonus_weight",
    "theme_etf_symbols",
    "theme_for_symbol",
    "theme_intelligence_enabled",
    "theme_momentum_scores",
]
