"""Tests for :mod:`src.signal_scoring`."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.signal_scoring import score_signal


def test_score_signal_empty_mapping() -> None:
    assert score_signal({}) == 0


def test_score_signal_all_true_mapping() -> None:
    s = {
        "trend": True,
        "pullback": True,
        "momentum": True,
        "volatility": True,
        "regime_ok": True,
        "spread_ok": True,
    }
    assert score_signal(s) == 100


@pytest.mark.parametrize(
    "key,points",
    [
        ("trend", 25),
        ("pullback", 20),
        ("momentum", 20),
        ("volatility", 15),
        ("regime_ok", 10),
        ("spread_ok", 10),
    ],
)
def test_score_signal_single_flag(key: str, points: int) -> None:
    assert score_signal({key: True}) == points


def test_score_signal_object_attributes() -> None:
    @dataclass
    class S:
        trend: bool = True
        pullback: bool = False
        momentum: bool = True
        volatility: bool = False
        regime_ok: bool = True
        spread_ok: bool = False

    assert score_signal(S()) == 25 + 20 + 10


def test_score_signal_false_and_none_mapping() -> None:
    assert (
        score_signal(
            {
                "trend": False,
                "pullback": None,
                "momentum": 0,
                "volatility": "",
                "regime_ok": False,
                "spread_ok": False,
            }
        )
        == 0
    )
