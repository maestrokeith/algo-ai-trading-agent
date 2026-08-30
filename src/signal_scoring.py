"""Additive score (0–100) for entry-side gate booleans (trend, pullback, momentum, etc.)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict


class SignalScoreFields(TypedDict, total=False):
    """Optional booleans for :func:`score_signal` (mapping or attribute-backed object)."""

    trend: bool
    pullback: bool
    momentum: bool
    volatility: bool
    regime_ok: bool
    spread_ok: bool


def _truthy(signal: Any, key: str) -> bool:
    if isinstance(signal, Mapping):
        return bool(signal.get(key))
    return bool(getattr(signal, key, False))


def score_signal(signal: Any) -> int:
    """
    Sum weighted contributions when each gate flag is true (max 100).

    Accepts a :class:`SignalScoreFields`-like mapping or any object with the same
    attribute names. Missing / false / none-like values add 0 for that bucket.
    """
    score = 0

    if _truthy(signal, "trend"):
        score += 25
    if _truthy(signal, "pullback"):
        score += 20
    if _truthy(signal, "momentum"):
        score += 20
    if _truthy(signal, "volatility"):
        score += 15
    if _truthy(signal, "regime_ok"):
        score += 10
    if _truthy(signal, "spread_ok"):
        score += 10

    return score
