from __future__ import annotations

from src.sector_config import parse_sector_config, resolve_sector_key_for_sizing


def test_parse_sector_config_defaults() -> None:
    d = parse_sector_config({})
    assert d["default_sector"] == "unknown"
    assert d["enforce_caps_on_unknown"] is True


def test_parse_sector_config_yaml_style() -> None:
    d = parse_sector_config(
        {"sector": {"default_sector": "other", "enforce_caps_on_unknown": False}}
    )
    assert d["default_sector"] == "other"
    assert d["enforce_caps_on_unknown"] is False


def test_resolve_explicit_vs_default() -> None:
    a, e = resolve_sector_key_for_sizing("NVDA", {"NVDA": "technology"}, default_sector="other")
    assert a == "technology" and e is True
    a2, e2 = resolve_sector_key_for_sizing("ZZZ", {"NVDA": "technology"}, default_sector="other")
    assert a2 == "other" and e2 is False
