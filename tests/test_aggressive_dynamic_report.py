from __future__ import annotations

import json
from pathlib import Path

from src.aggressive_dynamic_report import build_aggressive_dynamic_report, render_aggressive_dynamic_report
from src.trade_attribution import attribution_daily_path


def test_aggressive_dynamic_report_summarizes_candidates_orders_and_fills(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    path = attribution_daily_path(data_dir=data_dir, user_id="live_bot", day="2026-07-17")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "symbol": "LCID",
                        "aggressive_dynamic_mode": True,
                        "aggressive_dynamic_score": 62,
                        "normal_decision": "rejected",
                        "bypassed_noncritical_rules": ["market_vwap", "sector"],
                        "price": 3.5,
                        "catalyst_type": "news",
                    }
                ],
                "orders": [
                    {"symbol": "LCID", "aggressive_dynamic_mode": True, "status": "submitted"},
                    {"symbol": "LCID", "aggressive_dynamic_mode": True, "status": "filled"},
                ],
                "exits": [
                    {
                        "symbol": "LCID",
                        "aggressive_dynamic_mode": True,
                        "realized_pnl": 12.5,
                        "max_favorable_excursion_pct": 3.0,
                        "max_adverse_excursion_pct": -1.0,
                        "price_tier": "two_to_5",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = build_aggressive_dynamic_report(data_dir=data_dir, user_id="live_bot", day="2026-07-17")
    text = render_aggressive_dynamic_report(report)

    assert report["normal_rejected_aggressive_accepted"] == 1
    assert report["submitted"] == 2
    assert report["filled"] == 1
    assert report["winners"] == 1
    assert report["net_incremental_pnl"] == 12.5
    assert report["by_price_tier"]["two_to_5"] == 2
    assert "submitted=2 filled=1" in text
