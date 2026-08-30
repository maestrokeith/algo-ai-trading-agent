"""Tests for daily trade postmortem analysis."""

from __future__ import annotations

from pathlib import Path

from src.trade_postmortem import (
    build_daily_postmortem,
    explain_trade,
    render_daily_postmortem_markdown,
    write_daily_postmortem_report,
)


def test_explain_trade_describes_dynamic_winner_with_catalyst() -> None:
    item = explain_trade(
        {
            "symbol": "CRWD",
            "strategy": "dynamic_universe",
            "pnl": 120.0,
            "return_pct": 6.0,
            "catalyst_type": "ai",
            "news_score": 8,
            "event_score": 7,
            "relative_volume": 2.4,
            "momentum_score": 0.82,
            "regime_score": 4,
            "exit_reason": "profit_target",
        }
    )

    assert item is not None
    assert item.outcome == "winner"
    assert "dynamic candidate" in item.explanation
    assert "ai catalyst" in item.explanation
    assert "strong news score" in item.explanation
    assert "entry rationale" in item.explanation
    assert "exit rationale: profit target" in item.explanation


def test_build_daily_postmortem_suggests_dynamic_threshold_review() -> None:
    review = build_daily_postmortem(
        [
            {"symbol": "A", "strategy": "dynamic_universe", "pnl": -10.0, "news_score": 3, "return_pct": -1.0},
            {"symbol": "B", "strategy": "dynamic_universe", "pnl": -20.0, "news_score": 0, "return_pct": -2.0},
            {"symbol": "C", "strategy": "trend_long", "pnl": 5.0, "return_pct": 1.0},
        ]
    )

    assert [row.symbol for row in review.losers] == ["B", "A"]
    assert review.winners[0].symbol == "C"
    assert any("dynamic entry thresholds" in item for item in review.suggestions)
    assert any("stronger news scores" in item for item in review.suggestions)


def test_build_daily_postmortem_neutral_when_no_actionable_issue() -> None:
    review = build_daily_postmortem(
        [{"symbol": "A", "strategy": "trend_long", "pnl": 10.0, "return_pct": 2.0}]
    )

    assert review.winners[0].symbol == "A"
    assert review.suggestions == ["No parameter changes suggested from today's trade set."]


def test_render_daily_postmortem_markdown_includes_required_metrics() -> None:
    markdown = render_daily_postmortem_markdown(
        [
            {
                "symbol": "CRWD",
                "strategy": "dynamic_universe",
                "pnl": 100.0,
                "return_pct": 5.0,
                "hold_minutes": 45,
                "catalyst_type": "ai",
                "news_score": 8,
                "relative_volume": 2.0,
                "exit_reason": "profit_target",
                "max_favorable_excursion_pct": 8.0,
            },
            {
                "symbol": "NVTS",
                "strategy": "dynamic_universe",
                "pnl": -50.0,
                "return_pct": -2.5,
                "hold_minutes": 15,
                "news_score": 2,
                "exit_reason": "manual_exit",
            },
        ],
        report_date="2026-06-05",
        user_id="u1",
    )

    assert "# Trade Postmortem - 2026-06-05" in markdown
    assert "Win rate: 50.00%" in markdown
    assert "Average winner: $100.00" in markdown
    assert "Average loser: $-50.00" in markdown
    assert "Profit factor: 2.00" in markdown
    assert "Average hold time: 30.0 minutes" in markdown
    assert "Biggest Missed Winners" in markdown
    assert "Biggest Avoidable Losers" in markdown


def test_write_daily_postmortem_report(tmp_path: Path) -> None:
    out = write_daily_postmortem_report(
        [{"symbol": "AAPL", "strategy": "trend_long", "pnl": 10.0, "return_pct": 1.0}],
        report_date="2026-06-05",
        reports_dir=tmp_path,
    )

    assert out == tmp_path / "2026-06-05.md"
    assert "AAPL was a winner" in out.read_text(encoding="utf-8")
