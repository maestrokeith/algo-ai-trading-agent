from __future__ import annotations

import json
from pathlib import Path

from scripts.show_daily_summary import main
from src.combined_daily_summary import build_combined_daily_summary, format_combined_daily_summary
from src.trade_attribution import attribution_daily_path


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_daily_artifacts(tmp_path: Path) -> None:
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-06"),
        {
            "version": 1,
            "date": "2026-06-06",
            "user_id": "live_bot",
            "orders": [
                {"symbol": "AAPL", "action": "buy", "submitted": True},
                {"symbol": "AAPL", "action": "sell", "submitted": True},
            ],
            "exits": [
                {
                    "symbol": "AAPL",
                    "entry_route": "core_rebuild",
                    "pnl": 120.0,
                    "pnl_pct": 4.0,
                    "exit_reason": "profit_target",
                    "hold_minutes": 90,
                    "catalyst_type": "earnings",
                    "news_score": 8,
                },
                {
                    "symbol": "NVTS",
                    "entry_route": "dynamic_universe",
                    "pnl": -45.0,
                    "pnl_pct": -2.0,
                    "exit_reason": "signal_flip",
                    "hold_minutes": 12,
                    "news_score": 2,
                },
            ],
        },
    )
    _write_json(
        tmp_path / "daily_summary" / "2026-06-06_live_bot.json",
        {"unrealized_pnl": 15.0},
    )
    _write_json(
        tmp_path / "analytics" / "catalyst_outcomes.json",
        {
            "outcomes": [
                {
                    "user_id": "live_bot",
                    "symbol": "AAPL",
                    "catalyst_type": "earnings",
                    "realized_return_pct": 4.0,
                },
                {
                    "user_id": "live_bot",
                    "symbol": "NVTS",
                    "catalyst_type": "news",
                    "realized_return_pct": -2.0,
                },
            ]
        },
    )
    _write_json(
        tmp_path / "replay_market_session" / "2026-06-06_live_bot.json",
        {
            "clock": {"tick_count": 5, "cycles_with_data": 3},
            "mock_orders": [
                {"symbol": "AAPL", "side": "buy"},
                {"symbol": "AAPL", "side": "sell"},
            ],
            "selected_candidates": [{"symbol": "AAPL"}],
            "rejected_candidates": [{"symbol": "NVTS"}],
            "route_level_pnl_estimate": {"core_rebuild": 100.0, "dynamic_universe": -20.0},
            "churn_same_day_reversal_stats": {
                "same_day_reversal_count": 1,
                "repeat_order_count": 1,
            },
        },
    )


def test_combined_daily_summary_includes_required_sections(tmp_path: Path) -> None:
    _seed_daily_artifacts(tmp_path)

    report = build_combined_daily_summary(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-06",
    )
    text = format_combined_daily_summary(report)

    assert "Daily Summary 2026-06-06 [live_bot]" in text
    assert "PnL: realized=$75.00 unrealized=$15.00 total=$90.00" in text
    assert "Postmortem: trades=2 win_rate=50.0%" in text
    assert "Attribution: core_rebuild $120.00" in text
    assert "Churn: reversals=1 repeat_buys=1 repeat_sells=1 weak_exits=1" in text
    assert "Catalysts: earnings n=1 win=100.0% avg=4.0%" in text
    assert "Replay: ticks=5 cycles=3 mock_orders=2 selected=1 rejected=1" in text
    assert "Top winners: AAPL core_rebuild $120.00" in text
    assert "Top losers: NVTS dynamic_momentum $-45.00" in text


def test_combined_daily_summary_reports_weak_catalyst_dynamic_attribution(tmp_path: Path) -> None:
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-24"),
        {
            "version": 1,
            "date": "2026-06-24",
            "user_id": "live_bot",
            "exits": [
                {
                    "symbol": "DFTX",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": -74.21,
                    "pnl_pct": -3.4,
                    "news_score": 0,
                    "event_score": 0,
                    "catalyst_score": 0,
                },
                {
                    "symbol": "SOFI",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": 12.0,
                    "pnl_pct": 1.1,
                    "news_score": 0,
                    "event_score": 0,
                    "catalyst_score": 0,
                },
                {
                    "symbol": "CATX",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": 20.0,
                    "pnl_pct": 2.0,
                    "news_score": 7,
                    "event_score": 0,
                    "catalyst_score": 0,
                },
            ],
        },
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-24",
    )
    text = format_combined_daily_summary(report)

    assert report["weak_catalyst_dynamic"]["trades"] == 2
    assert report["weak_catalyst_dynamic"]["pnl"] == -62.21
    assert report["weak_catalyst_dynamic"]["win_rate"] == 0.5
    assert "Weak catalyst dynamic: trades=2 pnl=$-62.21 win=50.0%" in text


def test_combined_daily_summary_includes_dynamic_weak_catalyst_review(tmp_path: Path) -> None:
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-25"),
        {
            "exits": [
                {
                    "symbol": "WXYZ",
                    "entry_route": "dynamic_momentum_override",
                    "pnl": -12.5,
                    "weak_catalyst": True,
                }
            ]
        },
    )
    log_path = tmp_path / "logs" / "live_2026-06-25.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(
            [
                "2026-06-25 10:01:00 DYNAMIC_WEAK_CATALYST_CLASSIFIED symbol=WXYZ",
                "2026-06-25 10:02:00 DYNAMIC_WEAK_CATALYST_SIZE_REDUCED symbol=WXYZ notional_after=375.00",
                "2026-06-25 10:03:00 ORDER_SUBMITTED symbol=WXYZ side=buy weak_catalyst=true notional=375.00",
            ]
        ),
        encoding="utf-8",
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-25",
    )
    text = format_combined_daily_summary(report)

    assert report["dynamic_weak_catalyst_review"]["classified"] == 1
    assert report["dynamic_weak_catalyst_review"]["size_reduced"] == 1
    assert report["dynamic_weak_catalyst_review"]["realized_pnl"] == -12.5
    assert "Dynamic weak catalyst review: classified=1 rejected=0 size_reduced=1 orders=1 pnl=$-12.50 recommendation=leave unchanged" in text


def test_combined_daily_summary_handles_missing_artifacts(tmp_path: Path) -> None:
    report = build_combined_daily_summary(
        data_dir=tmp_path,
        user_id="default",
        day="2026-06-06",
    )
    text = format_combined_daily_summary(report)

    assert "PnL: realized=$0.00 unrealized=$0.00 total=$0.00" in text
    assert "Catalysts: no catalyst outcomes recorded." in text
    assert "Replay: no replay summary found." in text


def test_combined_daily_summary_counts_live_journal_order_activity(tmp_path: Path) -> None:
    log_path = tmp_path / "logs" / "live_2026-06-16.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(
            [
                "2026-06-16 10:05:00 ALLOCATOR_ACTION_SUBMITTED symbol=ADPT action=buy notional=1200.00 order_id=adpt-1 route=dynamic_momentum_override",
                "2026-06-16 10:05:01 ORDER_SUBMITTED symbol=ADPT side=buy notional=1200.00 source=capital_allocator order_id=adpt-1 status=accepted",
                "2026-06-16 10:05:02 ORDER_FILLED symbol=ADPT side=buy filled_qty=100 filled_avg_price=12.00 order_id=adpt-1",
                "2026-06-16 11:15:00 ALLOCATOR_ACTION_SUBMITTED symbol=ADPT action=buy notional=800.00 order_id=adpt-2 route=dynamic_universe",
                "2026-06-16 11:15:01 ORDER_SUBMITTED symbol=ADPT side=buy notional=800.00 source=capital_allocator order_id=adpt-2 status=accepted",
                "2026-06-16 12:03:00 ALLOCATOR_ACTION_SUBMITTED symbol=OPRA action=buy notional=1000.00 order_id=opra-1 route=dynamic_momentum_override",
                "2026-06-16 12:03:01 ORDER_SUBMITTED symbol=OPRA side=buy notional=1000.00 source=capital_allocator order_id=opra-1 status=accepted",
                "2026-06-16 15:55 ET PAYO SELL 17 shares \u2014 dynamic_eod_flatten",
            ]
        ),
        encoding="utf-8",
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        day="2026-06-16",
    )
    text = format_combined_daily_summary(report)

    assert report["inputs"]["live_activity_available"] is True
    assert report["order_activity"]["submitted_orders"] == 3
    assert report["order_activity"]["buy_orders"] == 3
    assert report["order_activity"]["sell_orders"] == 1
    assert report["order_activity"]["symbols"] == ["ADPT", "OPRA", "PAYO"]
    assert report["order_activity"]["route_counts"]["dynamic_momentum_override"] >= 2
    assert report["order_activity"]["route_counts"]["dynamic_universe"] >= 1
    assert report["order_activity"]["route_counts"]["dynamic_eod_flatten"] >= 1
    assert "Postmortem: trades=1" in text
    assert "Activity: submitted_orders=3 buys=3 sells=1 exits=1 pnl_missing_exits=1 symbols=ADPT,OPRA,PAYO" in text
    assert "PnL: realized=$0.00 unrealized=$0.00 total=$0.00" in text


def test_combined_daily_summary_includes_broker_open_unrealized_pnl(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "daily_summary" / "2026-06-26_live_bot.json",
        {
            "positions": [
                {"symbol": "IWM", "unrealized_pl": 18.25},
                {"symbol": "XLF", "unrealized_pl": 18.61},
            ]
        },
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-26",
    )
    text = format_combined_daily_summary(report)

    assert report["broker_open_unrealized"]["unrealized"] == 36.86
    assert report["profitability"]["overall_pnl"]["unrealized"] == 36.86
    assert "PnL: realized=$0.00 unrealized=$36.86 total=$36.86" in text
    assert "Unrealized source: broker_open_positions broker_open_unrealized=$36.86 positions=2" in text


def test_combined_daily_summary_reports_live_risk_guards(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "risk_guards" / "2026-06-26_live_bot.json",
        {
            "triggered_guards": ["trend_long_consecutive_losses"],
            "trend_long_entries_blocked": True,
            "new_entries_blocked": False,
            "flatten_risk": False,
            "loss_pct_equity": -0.12,
        },
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-26",
        include_journal=False,
    )
    text = format_combined_daily_summary(report)

    assert "Risk guards: triggered=trend_long_consecutive_losses" in text
    assert "trend_long_blocked=true" in text


def test_combined_daily_summary_reports_mfe_mae(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "trade_attribution" / "daily" / "2026-06-26_live_bot.json",
        {
            "orders": [],
            "exits": [
                {"symbol": "AAPL", "entry_route": "trend_long", "pnl": 1.0, "mfe_pct": 2.0, "mae_pct": -0.5},
                {"symbol": "MSFT", "entry_route": "trend_long", "pnl": -1.0, "mfe_pct": 1.0, "mae_pct": -1.5},
            ],
        },
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path,
        user_id="live_bot",
        day="2026-06-26",
        include_journal=False,
    )
    text = format_combined_daily_summary(report)

    assert report["mfe_mae"]["avg_mfe_pct"] == 1.5
    assert report["mfe_mae"]["avg_mae_pct"] == -1.0
    assert "MFE/MAE: avg_mfe=1.5% avg_mae=-1.0% count=2" in text


def test_combined_daily_summary_reduces_missing_exit_pnl_when_fill_metadata_exists(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "logs" / "live_2026-06-26.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(
            [
                "2026-06-26 09:40:00 ORDER_SUBMITTED symbol=MU side=buy notional=1000.00 order_id=mu-buy status=accepted route=dynamic_momentum_override",
                "2026-06-26 09:40:01 ORDER_FILLED symbol=MU side=buy filled_qty=10 filled_avg_price=100.00 order_id=mu-buy",
                "2026-06-26 09:48:00 ORDER_SUBMITTED symbol=MU side=sell qty=10 order_id=mu-sell status=accepted route=dynamic_momentum_override reason=stop_loss",
                "2026-06-26 09:48:01 ORDER_FILLED symbol=MU side=sell filled_qty=10 filled_avg_price=94.00 order_id=mu-sell",
            ]
        ),
        encoding="utf-8",
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path / "data",
        user_id="live_bot",
        day="2026-06-26",
    )
    text = format_combined_daily_summary(report)

    assert report["order_activity"]["pnl_missing_exits"] == 0
    assert report["order_activity"]["diagnostics"]["count_sources"]["broker_fill_logs"] == 2
    assert "PnL: realized=$-60.00 unrealized=$0.00 total=$-60.00" in text
    assert "Activity sources:" in text
    assert "broker_fill_logs=2" in text


def test_combined_daily_summary_counts_paper_review_dynamic_completed_trades(tmp_path: Path) -> None:
    log_path = tmp_path / "data" / "review" / "2026-06-16" / "paper_full.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "\n".join(
            [
                "2026-06-16 10:04:00 DYNAMIC_ALLOCATOR_INPUT symbol=AAL route=dynamic_momentum_override source=dynamic_universe score=2.70 gain=2.845 rel=0.755 catalyst_score=0.90466 news_score=9.0466 event_score=9.00",
                "2026-06-16 10:04:01 ALLOCATOR_ACTION_SUBMITTED symbol=AAL action=buy notional=4633.19 order_id=aal-buy route=dynamic_momentum_override",
                "2026-06-16 10:04:02 ORDER_SUBMITTED symbol=AAL side=buy notional=4633.19 source=capital_allocator order_id=aal-buy status=accepted",
                "2026-06-16 10:04:03 ORDER_FILLED symbol=AAL side=buy filled_qty=291.394968553 filled_avg_price=15.90 order_id=aal-buy",
                "2026-06-16 10:05:00 DYNAMIC_ALLOCATOR_INPUT symbol=AMKR route=dynamic_momentum_override source=dynamic_universe score=2.70 gain=6.950 rel=1.695 catalyst_score=0.991 news_score=9.91 event_score=9.00",
                "2026-06-16 10:05:01 ALLOCATOR_ACTION_SUBMITTED symbol=AMKR action=buy notional=4633.41 order_id=amkr-buy route=dynamic_momentum_override",
                "2026-06-16 10:05:02 ORDER_SUBMITTED symbol=AMKR side=buy notional=4633.41 source=capital_allocator order_id=amkr-buy status=accepted",
                "2026-06-16 10:05:03 ORDER_FILLED symbol=AMKR side=buy filled_qty=51.764048709 filled_avg_price=89.50 order_id=amkr-buy",
                "14:58 ET AMKR SELL 51.764048709 shares \u2014 stop_loss",
                "15:51 ET AAL SELL 291.394968553 shares \u2014 dynamic_eod_flatten",
            ]
        ),
        encoding="utf-8",
    )

    report = build_combined_daily_summary(
        data_dir=tmp_path / "data",
        user_id="paper_bot",
        day="2026-06-16",
    )
    text = format_combined_daily_summary(report)

    assert report["inputs"]["live_activity_available"] is True
    assert report["order_activity"]["buy_orders"] == 2
    assert report["order_activity"]["sell_orders"] == 2
    assert report["order_activity"]["exit_records"] == 2
    assert report["order_activity"]["pnl_missing_exits"] == 2
    assert report["order_activity"]["symbols"] == ["AAL", "AMKR"]
    assert report["profitability"]["route_stats"]["dynamic_momentum"]["trades"] == 2
    assert report["profitability"]["exit_reason_stats"]["dynamic_eod_flatten"]["exits"] == 1
    assert report["profitability"]["exit_reason_stats"]["stop_loss"]["exits"] == 1
    assert "Postmortem: trades=2" in text
    assert "Activity: submitted_orders=2 buys=2 sells=2 exits=2 pnl_missing_exits=2 symbols=AAL,AMKR" in text
    assert "Route stats: dynamic_momentum trades=2" in text


def test_show_daily_summary_cli_prints_combined_report(tmp_path: Path, capsys) -> None:
    _seed_daily_artifacts(tmp_path)

    assert main(["2026-06-06", "--user", "live_bot", "--data-dir", str(tmp_path), "--no-journal"]) == 0

    out = capsys.readouterr().out
    assert "Daily Summary 2026-06-06 [live_bot]" in out
    assert "Replay churn: reversals=1 repeat_orders=1" in out


def test_show_daily_summary_cli_latest_uses_newest_available_report_date(tmp_path: Path, capsys) -> None:
    _seed_daily_artifacts(tmp_path)
    _write_json(
        attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-07"),
        {
            "version": 1,
            "date": "2026-06-07",
            "user_id": "live_bot",
            "orders": [],
            "exits": [{"symbol": "MSFT", "entry_route": "trend_long", "pnl": 5.0}],
        },
    )

    assert main(["latest", "--user", "live_bot", "--data-dir", str(tmp_path), "--no-journal"]) == 0

    out = capsys.readouterr().out
    assert "Daily Summary 2026-06-07 [live_bot]" in out
    assert "MSFT trend_long $5.00" in out


def test_show_daily_summary_cli_rejects_bad_date(tmp_path: Path, capsys) -> None:
    assert main(["06-06-2026", "--data-dir", str(tmp_path)]) == 2

    err = capsys.readouterr().err
    assert "Use date as YYYY-MM-DD" in err
