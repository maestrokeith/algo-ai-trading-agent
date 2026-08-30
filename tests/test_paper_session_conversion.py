from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts import generate_paper_session_conversion_report as report_mod


def test_paper_session_conversion_report_includes_order_skip_stage(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def fake_run_replay(**_kwargs: Any) -> dict[str, Any]:
        return {
            "date": "2026-06-23",
            "summary_path": "data/replay/2026-06-23_paper_bot.json",
            "history_user": "paper_bot",
            "selected_candidates": [{"symbol": "BOLD"}],
            "allocator_actions_created": [{"symbol": "BOLD", "action": "buy"}],
            "simulated_submitted_orders": [],
            "per_symbol_trace": [
                {
                    "symbol": "BOLD",
                    "entry_eval": {"result": True, "reason": "ok"},
                    "allocator_candidate": {"result": True, "reason": "accepted"},
                    "allocator_action": {"result": True, "reason": "created"},
                    "trade_cycle_allowed": {"result": True, "reason": "none"},
                    "order_skip": {
                        "result": True,
                        "reason": "dynamic_price_below_minimum",
                        "source_stage": "order_dispatch",
                    },
                    "simulated_submit": {
                        "result": False,
                        "reason": "dynamic_price_below_minimum",
                    },
                    "final_stage": "order_skip",
                    "final_reason": "dynamic_price_below_minimum",
                }
            ],
        }

    def fake_config(_project_root: Path, _user: str) -> Mapping[str, Any]:
        return {
            "portfolio": {
                "capital_allocator": {
                    "allow_no_trade_cycles": False,
                    "require_net_sell_gte_buy": False,
                    "selected_must_execute": False,
                    "min_trade_size": 500,
                    "min_realloc_leg": 300,
                    "minimum_cash_to_deploy_pct": 0.0,
                }
            }
        }

    monkeypatch.setattr(report_mod.replay_live_cycle, "run_replay", fake_run_replay)
    monkeypatch.setattr(report_mod.replay_live_cycle, "_load_replay_config", fake_config)

    report = report_mod.build_paper_session_conversion_report(
        project_root=tmp_path,
        date="2026-06-23",
        user="paper_bot",
    )

    row = report["candidates"][0]
    assert row["symbol"] == "BOLD"
    assert row["selected"]["result"] is True
    assert row["entry_eval_pass"] == {"result": True, "reason": "ok", "detail": {}}
    assert row["allocator_input"]["result"] is True
    assert row["allocator_action"]["result"] is True
    assert row["order_skip"] == {
        "result": True,
        "reason": "dynamic_price_below_minimum",
        "detail": {"source_stage": "order_dispatch"},
    }
    assert row["simulated_order_submitted"]["result"] is False
    assert row["final_stage"] == "order_skip"
    assert row["final_reason"] == "dynamic_price_below_minimum"

    rendered = report_mod.render_paper_session_conversion_report(report)
    assert "Entry Eval Pass" in rendered
    assert "Order Skip" in rendered
    assert "| BOLD | True | True | True | True | True | False | order_skip | dynamic_price_below_minimum |" in rendered
