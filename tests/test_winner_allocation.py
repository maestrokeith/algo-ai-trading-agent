"""Tests for :mod:`src.winner_allocation`."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from src.winner_allocation import (
    apply_winner_size_multiplier_to_trend_row,
    mark_top_signal_symbols_in_chosen,
    parse_winner_allocation_config,
)


def test_parse_winner_allocation_disabled_by_default() -> None:
    en, n, m = parse_winner_allocation_config({})
    assert en is False
    assert n == 0
    assert m == pytest.approx(1.5)


def test_parse_winner_top_n_defaults_to_one_when_enabled() -> None:
    en, n, _m = parse_winner_allocation_config(
        {"position_sizing": {"winner_allocation": {"enabled": True}}}
    )
    assert en is True
    assert n == 1


def test_parse_winner_allocation_from_yaml_shape() -> None:
    en, n, m = parse_winner_allocation_config(
        {
            "position_sizing": {
                "winner_allocation": {
                    "enabled": True,
                    "top_n": 2,
                    "size_multiplier": 1.5,
                }
            }
        }
    )
    assert en is True
    assert n == 2
    assert m == pytest.approx(1.5)


def test_mark_top_signal_symbols_in_chosen() -> None:
    rows = [
        {"sym_u": "ZZZ", "strength_eff": 0.9},
        {"sym_u": "AAA", "strength_eff": 0.5},
    ]
    s = mark_top_signal_symbols_in_chosen(
        rows, top_n=1, size_multiplier=1.5, sym_key="sym_u"
    )
    assert s == {"ZZZ"}
    assert rows[0].get("in_top_signals") is True
    assert rows[0].get("winner_size_multiplier") == pytest.approx(1.5)
    assert "in_top_signals" not in rows[1]


def test_apply_winner_size_multiplier_scales_shares_and_order() -> None:
    @dataclass
    class _PS:
        shares: int
        notional: float
        risk_amount: float
        risk_pct: float
        exposure_pct: float

    ps = _PS(
        shares=10,
        notional=1000.0,
        risk_amount=50.0,
        risk_pct=0.2,
        exposure_pct=0.1,
    )
    d = SimpleNamespace(allowed=True, position_sizing=ps, order_request=MagicMock())
    engine = MagicMock()
    new_order = MagicMock()
    engine.execution.build_order_for_entry.return_value = new_order
    row = {
        "sym_u": "NVDA",
        "winner_size_multiplier": 1.5,
        "decision": d,
        "df": pd.DataFrame({"close": [200.0]}),
        "quote": SimpleNamespace(
            spread_pct=0.1, bid=199.0, ask=201.0, skip_spread_check=False
        ),
        "notional": 1000.0,
    }
    apply_winner_size_multiplier_to_trend_row(row, engine=engine)
    assert d.position_sizing.shares == 15
    assert d.position_sizing.notional == pytest.approx(1500.0)
    assert d.order_request is new_order
    assert row["notional"] == pytest.approx(1500.0)
    engine.execution.build_order_for_entry.assert_called_once()
    c = engine.execution.build_order_for_entry.call_args
    assert c[0][2] == 15
