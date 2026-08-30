"""Tests for the live options summary script."""

from __future__ import annotations

import sys
from types import SimpleNamespace


def test_show_options_summary_prints_kill_switch(monkeypatch, capsys, tmp_path) -> None:
    import scripts.show_options_summary as sp

    class _Broker:
        def __init__(self, config):
            self.paper = True

        def get_positions(self):
            return [
                {
                    "symbol": "HPE260619C00020000",
                    "qty": 2,
                    "avg_entry_price": 1.50,
                    "cost_basis": -300.0,
                    "market_value": 360.0,
                    "unrealized_pl": 60.0,
                }
            ]

        def get_equity(self):
            return 100_000.0

        def get_option_latest_quote(self, symbol):
            return SimpleNamespace(mid=1.8, timestamp=None)

    monkeypatch.setattr(sp, "AlpacaBroker", _Broker)
    monkeypatch.setattr(sp, "load_config", lambda path: {"broker": {"paper": True}, "options": {"enabled": True, "mode": "paper_only"}})
    monkeypatch.setattr(sys, "argv", ["show_options_summary.py", "--paper", "--data-dir", str(tmp_path / "data")])

    sp.main()
    out = capsys.readouterr().out
    assert "open option positions: 1" in out
    assert "kill_switch=" in out
    assert "HPE260619C00020000" in out
