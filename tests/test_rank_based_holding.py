from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.rank_based_holding import (
    keep_top_n_sell_rest,
    parse_rank_based_holding_cfg,
    rank_positions,
    sell_rest_worst_first,
)


def test_keep_top_n_sell_rest() -> None:
    ranked = ["A", "B", "C", "D", "E"]
    k, s = keep_top_n_sell_rest(ranked, top_n=2)
    assert k == ["A", "B"]
    assert s == ["C", "D", "E"]


def test_sell_rest_worst_first() -> None:
    assert sell_rest_worst_first(["C", "D", "E"]) == ["E", "D", "C"]


def test_parse_rank_based_holding_cfg_defaults() -> None:
    d = parse_rank_based_holding_cfg({})
    assert d["enabled"] is False
    assert d["top_n"] == 0
    assert d["max_sells_per_pass"] == 2


def test_parse_rank_based_holding_cfg_explicit() -> None:
    d = parse_rank_based_holding_cfg(
        {
            "rank_based_holding": {
                "enabled": True,
                "top_n": 8,
                "max_sells_per_pass": 3,
            }
        }
    )
    assert d["enabled"] is True
    assert d["top_n"] == 8
    assert d["max_sells_per_pass"] == 3


def test_rank_positions_orders_desc_strength(monkeypatch: pytest.MonkeyPatch) -> None:
    strengths = {"A": 0.1, "B": 0.5, "C": 0.2}

    def _fake(
        su: str,
        tracked: object,
        positions: object,
        *,
        get_bars: object,
        engine: object,
        rep_sub: object = None,
        **_: object,
    ) -> float:
        return float(strengths.get(su, 0.0))

    monkeypatch.setattr("src.rank_based_holding.replacement_hold_strength", _fake)
    r = rank_positions(
        ["C", "A", "B"],
        {},
        [],
        get_bars=MagicMock(),
        engine=MagicMock(),
    )
    assert r == ["B", "C", "A"]


def test_rank_positions_tie_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    def _same(
        su: str,
        tracked: object,
        positions: object,
        *,
        get_bars: object,
        engine: object,
        rep_sub: object = None,
        **_: object,
    ) -> float:
        return 1.0

    monkeypatch.setattr("src.rank_based_holding.replacement_hold_strength", _same)
    r = rank_positions(
        ["Z", "A", "M"],
        {},
        [],
        get_bars=MagicMock(),
        engine=MagicMock(),
    )
    assert r == ["A", "M", "Z"]


def test_max_portfolio_positions_uses_key_in_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """When top_n is 0, live pass uses max_portfolio_positions; pure parse leaves top_n=0."""
    p = parse_rank_based_holding_cfg(
        {
            "max_positions": 7,
            "rank_based_holding": {"enabled": True, "top_n": 0, "max_sells_per_pass": 1},
        }
    )
    assert p["top_n"] == 0
