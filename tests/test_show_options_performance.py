"""Tests for the paper options performance report script."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace


def test_show_options_performance_prints_summary_and_gate(monkeypatch, capsys, tmp_path: Path) -> None:
    import scripts.show_options_performance as sp

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    trades = [
        {
            "symbol": "HPE260619C00020000",
            "status": "closed",
            "entry_reason": "source=dynamic_universe; catalyst=earnings",
            "exit_reason": "option_profit_take",
            "realized_pl": 100.0,
            "premium_paid": 300.0,
            "entry_quote_spread_pct": 6.0,
            "intended_limit_price": 1.50,
            "entry_fill_price": 1.53,
            "entry_time": "2026-06-01T13:00:00+00:00",
            "exit_time": "2026-06-01T14:00:00+00:00",
        }
        for _ in range(30)
    ]
    state = {
        "meta": {"updated_at": "2026-06-02T14:30:00+00:00", "user_id": "default"},
        "positions": {},
        "history": trades,
        "daily": {
            "2026-05-28": {"kill_switch_on": False, "block_new_entries": False},
            "2026-05-29": {"kill_switch_on": False, "block_new_entries": False},
            "2026-05-30": {"kill_switch_on": False, "block_new_entries": False},
            "2026-06-01": {"kill_switch_on": False, "block_new_entries": False},
            "2026-06-02": {"kill_switch_on": False, "block_new_entries": False},
        },
    }
    with open(data_dir / "options_positions_default.json", "w") as f:
        json.dump(state, f, indent=2)

    class _Broker:
        def __init__(self, config):
            self.paper = True

        def get_positions(self):
            return []

        def get_equity(self):
            return 100_000.0

        def get_orders_for_date(self, trade_date):
            return [{"symbol": "AAPL", "pnl": 50.0, "return_pct": 1.0}]

    monkeypatch.setattr(sp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sp, "AlpacaBroker", _Broker)
    monkeypatch.setattr(
        sp,
        "load_config",
        lambda path: {"broker": {"paper": True}, "options": {"enabled": True, "mode": "paper_only"}},
    )
    monkeypatch.setattr(sys, "argv", ["show_options_performance.py", "--paper"])

    sp.main()
    out = capsys.readouterr().out
    assert "OPTIONS_PERFORMANCE_SUMMARY" in out
    assert "OPTIONS_PROMOTION_GATE_PASS" in out
    assert "profit_factor=" in out
    assert "best_contract=" in out
    assert "Options performance dashboard" in out
    assert "Stock vs option edge" in out


def test_show_options_performance_prints_fail_reasons(monkeypatch, capsys, tmp_path: Path) -> None:
    import scripts.show_options_performance as sp

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "meta": {"updated_at": "2026-06-02T14:30:00+00:00", "user_id": "default"},
        "positions": {},
        "history": [],
        "daily": {"2026-06-02": {"kill_switch_on": True, "block_new_entries": True}},
    }
    with open(data_dir / "options_positions_default.json", "w") as f:
        json.dump(state, f, indent=2)

    class _Broker:
        def __init__(self, config):
            self.paper = True

        def get_positions(self):
            return []

        def get_equity(self):
            return 100_000.0

    monkeypatch.setattr(sp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sp, "AlpacaBroker", _Broker)
    monkeypatch.setattr(
        sp,
        "load_config",
        lambda path: {"broker": {"paper": True}, "options": {"enabled": True, "mode": "paper_only"}},
    )
    monkeypatch.setattr(sys, "argv", ["show_options_performance.py", "--paper"])

    sp.main()
    out = capsys.readouterr().out
    assert "OPTIONS_PROMOTION_GATE_FAIL" in out
    assert "keep_options_mode=paper_only" in out
    assert "need at least 30 closed paper option trades" in out
