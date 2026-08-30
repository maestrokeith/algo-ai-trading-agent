from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.strategy_quality_report import build_strategy_quality_report, render_strategy_quality_report
from src.trade_attribution import (
    record_exit,
    record_rejected_one_rule,
)


def test_rejected_one_rule_persistence_and_strategy_quality_report(tmp_path: Path) -> None:
    ts = datetime(2026, 7, 8, 15, 45, tzinfo=timezone.utc)
    record_exit(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=ts,
        symbol="MSFT",
        qty=3,
        exit_reason="stop_loss",
        pnl=-25.0,
        pnl_pct=-1.2,
        hold_minutes=14,
        entry_route="trend_long",
        entry_source="scanner",
        mfe_pct=0.4,
        mae_pct=-1.4,
        market_regime_score=2,
        market_regime_label="defensive",
        spy_above_vwap=False,
        qqq_above_vwap=False,
        symbol_above_vwap=True,
        sector_above_vwap=False,
        relative_volume=0.8,
        spread_pct=0.2,
        day_gain_pct=1.1,
        atr_expansion=1.3,
        vwap_distance_pct=0.4,
        alignment_1m=True,
        alignment_5m=False,
        trend_15m=False,
        catalyst_score=0,
        news_score=0,
        event_score=0,
        article_count=0,
        premarket_injected=False,
        trend_long_quality_score=4,
        entry_quality_score=82,
        entry_quality_penalties=["market_vwap_penalty=-8"],
        entry_quality_adaptive_market_vwap=True,
        adaptive_entry=True,
    )
    record_rejected_one_rule(
        data_dir=tmp_path,
        user_id="live_bot",
        timestamp=ts,
        symbol="BABX",
        rejected_rule="symbol_vwap_not_confirmed",
        price=10.5,
        features={"trend_long_quality_score": 5, "symbol_above_vwap": False},
    )

    report = build_strategy_quality_report(
        data_dir=tmp_path,
        reports_dir=tmp_path / "reports",
        user_id="live_bot",
        day="2026-07-08",
    )
    text = render_strategy_quality_report(report)
    dashboard = Path(report["dashboard_path"])

    assert report["pnl_by_sleeve"]["trend_long"] == -25.0
    assert report["rejected_one_rule"]["by_rule"] == {"symbol_vwap_not_confirmed": 1}
    assert report["trend_long_quality_score_distribution"]["avg"] == 4.0
    assert report["entry_quality_adaptive_scoring"]["average_entry_score"] == 82.0
    assert report["entry_quality_adaptive_scoring"]["adaptive_entries"] == 1
    assert report["entry_quality_adaptive_scoring"]["adaptive_entries_pnl"] == -25.0
    assert report["entry_quality_adaptive_scoring"]["reasons_removed_by_adaptive_scoring"] == {"market_vwap_penalty=-8": 1}
    assert dashboard.exists()
    assert json.loads(dashboard.read_text(encoding="utf-8"))["dynamic_no_catalyst_results"]["trades"] == 0
    assert "Strategy Quality Report 2026-07-08 [live_bot]" in text
    assert "Entry quality adaptive scoring:" in text
