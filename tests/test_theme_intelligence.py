"""Tests for sector/theme intelligence helpers."""

from __future__ import annotations

from src.market.theme_intelligence import (
    load_theme_definitions,
    symbol_theme_bonus,
    theme_etf_symbols,
    theme_for_symbol,
    theme_momentum_scores,
)


def test_default_themes_include_required_buckets() -> None:
    themes = load_theme_definitions({})

    assert {"ai", "semiconductors", "cybersecurity", "energy", "biotech"} <= set(themes)
    assert "SMH" in themes["semiconductors"].etfs
    assert "CRWD" in themes["cybersecurity"].symbols
    assert "XBI" in theme_etf_symbols({})


def test_theme_momentum_scores_average_etf_gains() -> None:
    scores = theme_momentum_scores(
        {
            "SMH": {"day_gain_pct": 4.0},
            "SOXX": {"day_gain_pct": 2.0},
            "XLE": {"day_gain_pct": -1.0},
        },
        {},
    )

    assert scores["semiconductors"] == 3.0
    assert scores["energy"] == -1.0


def test_symbol_theme_bonus_uses_positive_matching_theme() -> None:
    cfg = {"theme_intelligence": {"enabled": True, "bonus_weight": 0.5, "max_bonus": 1.0}}

    theme, bonus = symbol_theme_bonus("AMD", {"semiconductors": 4.0}, cfg)

    assert theme == "semiconductors"
    assert bonus == 1.0
    assert theme_for_symbol("CRWD") == "cybersecurity"
    assert symbol_theme_bonus("XOM", {"energy": -2.0}, cfg) == ("energy", 0.0)
