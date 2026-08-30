from __future__ import annotations

from pathlib import Path
import json

from src.combined_daily_summary import build_combined_daily_summary, format_combined_daily_summary


def test_daily_summary_empty_day_smoke(tmp_path: Path) -> None:
    report = build_combined_daily_summary(data_dir=tmp_path, user_id="paper_bot", day="2026-06-16")
    text = format_combined_daily_summary(report)

    assert "Daily Summary 2026-06-16 [paper_bot]" in text
    assert "Postmortem: trades=0" in text


def test_daily_summary_labels_broker_open_unrealized_source(tmp_path: Path) -> None:
    summary_path = tmp_path / "daily_summary" / "2026-06-26_live_bot.json"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        json.dumps(
            {
                "positions": [
                    {"symbol": "IWM", "unrealized_pl": 18.25},
                    {"symbol": "XLF", "unrealized_pl": 18.61},
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_combined_daily_summary(data_dir=tmp_path, user_id="live_bot", day="2026-06-26")
    text = format_combined_daily_summary(report)

    assert report["profitability"]["overall_pnl"]["unrealized"] == 36.86
    assert "Unrealized source: broker_open_positions broker_open_unrealized=$36.86 positions=2" in text
