"""Tests for the option positions reporting script."""

from __future__ import annotations

import sys
from types import SimpleNamespace


def test_show_option_positions_prints_positions(monkeypatch, capsys) -> None:
    import scripts.show_option_positions as sp

    class _Broker:
        def __init__(self, config):
            self.paper = True

        def get_equity(self):
            return 100_000.0

        def get_positions(self):
            return [
                {
                    "symbol": "HPE260619C00020000",
                    "qty": 2,
                    "side": "long",
                    "cost_basis": -300.0,
                    "current_price": 1.8,
                    "market_value": 360.0,
                    "unrealized_pl": 60.0,
                }
            ]

        def get_option_latest_quote(self, symbol):
            return SimpleNamespace(mid=1.8)

    monkeypatch.setattr(sp, "AlpacaBroker", _Broker)
    monkeypatch.setattr(sp, "load_config", lambda path: {"broker": {"paper": True}})
    monkeypatch.setattr(sys, "argv", ["show_option_positions.py", "--paper"])

    sp.main()
    out = capsys.readouterr().out
    assert "Open option positions: 1" in out
    assert "HPE260619C00020000" in out
    assert "TOTAL" in out

