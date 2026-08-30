"""Tests for regime.neutral_probe size floor."""

from __future__ import annotations

from src.regime_neutral_probe import apply_neutral_probe_size_floor


def test_disabled_noop() -> None:
    m, applied = apply_neutral_probe_size_floor(
        0.1,
        regime_condition="neutral",
        probe_cfg={"enabled": False, "size_multiplier": 0.3},
    )
    assert m == 0.1 and applied is False


def test_not_neutral_noop() -> None:
    m, applied = apply_neutral_probe_size_floor(
        0.1,
        regime_condition="defensive",
        probe_cfg={"enabled": True, "size_multiplier": 0.3},
    )
    assert m == 0.1 and applied is False


def test_floor_applied() -> None:
    m, applied = apply_neutral_probe_size_floor(
        0.1,
        regime_condition="neutral",
        probe_cfg={"enabled": True, "size_multiplier": 0.3},
    )
    assert m == 0.3 and applied is True


def test_no_raise_when_already_above_floor() -> None:
    m, applied = apply_neutral_probe_size_floor(
        0.5,
        regime_condition="neutral",
        probe_cfg={"enabled": True, "size_multiplier": 0.3},
    )
    assert m == 0.5 and applied is False
