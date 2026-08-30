"""Tests for catalyst outcome extraction and aggregation."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

from src.catalyst_outcomes import (
    append_catalyst_outcomes_json,
    build_historical_catalyst_outcomes,
    format_historical_catalyst_summary,
    historical_catalyst_outcome_path,
    latest_historical_catalyst_date,
    load_catalyst_outcome_records,
    outcome_from_trade,
    outcomes_from_trades,
    record_catalyst_outcomes_from_trades,
    summarize_historical_catalyst_outcomes,
    summarize_catalyst_outcomes,
    write_historical_catalyst_outcome_report,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def record_catalyst_outcome(self, **kwargs) -> None:
        self.rows.append(kwargs)


def test_outcome_from_trade_requires_catalyst_context_and_return() -> None:
    outcome = outcome_from_trade(
        {
            "id": "ord-1",
            "symbol": "CRWD",
            "strategy": "dynamic_universe",
            "catalyst_type": "ai",
            "news_score": 8,
            "catalyst_score": 0.9,
            "entry_price": 100,
            "exit_price": 104.5,
            "hold_minutes": 30,
            "return_pct": 4.5,
        },
        observed_date=date(2026, 6, 5),
    )

    assert outcome is not None
    assert outcome.symbol == "CRWD"
    assert outcome.catalyst_type == "ai"
    assert outcome.news_score == 8.0
    assert outcome.catalyst_score == 0.9
    assert outcome.entry_price == 100
    assert outcome.exit_price == 104.5
    assert outcome.hold_duration_minutes == 30
    assert outcome.subsequent_return_pct == 4.5
    assert outcome.trade_id == "ord-1"
    assert outcome_from_trade({"symbol": "AAPL", "return_pct": 1.0}, observed_date="2026-06-05") is None


def test_outcomes_from_trades_computes_return_from_pnl_and_notional() -> None:
    outcomes = outcomes_from_trades(
        [
            {
                "symbol": "NVDA",
                "source": "deal",
                "news_score": 6,
                "pnl": 50,
                "qty": 2,
                "filled_avg_price": 250,
            }
        ],
        observed_date="2026-06-05",
    )

    assert len(outcomes) == 1
    assert outcomes[0].subsequent_return_pct == 10.0


def test_record_and_summarize_catalyst_outcomes() -> None:
    store = _Store()
    count = record_catalyst_outcomes_from_trades(
        store,
        user_id="u1",
        observed_date="2026-06-05",
        trades=[
            {"symbol": "A", "catalyst_type": "ai", "news_score": 8, "return_pct": 4.0},
            {"symbol": "B", "catalyst_type": "ai", "news_score": 7, "return_pct": -2.0},
            {"symbol": "C", "catalyst_type": "deal", "news_score": 5, "return_pct": 1.0},
        ],
    )

    assert count == 3
    assert store.rows[0]["user_id"] == "u1"
    assert store.rows[0]["symbol"] == "A"
    summary = summarize_catalyst_outcomes(store.rows)
    assert summary["ai"]["count"] == 2.0
    assert summary["ai"]["sample_count"] == 2.0
    assert summary["ai"]["win_rate_pct"] == 50.0
    assert summary["ai"]["avg_return_pct"] == 1.0
    assert summary["ai"]["median_return_pct"] == 1.0
    assert summary["ai"]["profit_factor"] == 2.0
    assert summary["deal"]["win_rate_pct"] == 100.0


def test_json_store_appends_completed_outcomes_without_duplicates(tmp_path) -> None:
    path = tmp_path / "analytics" / "catalyst_outcomes.json"
    trades = [
        {
            "id": "t1",
            "symbol": "AVGO",
            "catalyst_type": "earnings",
            "catalyst_score": 0.8,
            "news_score": 8,
            "entry_price": 100,
            "exit_price": 110,
            "return_pct": 10.0,
            "hold_minutes": 40,
        }
    ]

    assert append_catalyst_outcomes_json(trades, observed_date="2026-06-05", user_id="u1", path=path) == 1
    assert append_catalyst_outcomes_json(trades, observed_date="2026-06-05", user_id="u1", path=path) == 0
    records = load_catalyst_outcome_records(path)

    assert len(records) == 1
    assert records[0]["symbol"] == "AVGO"
    assert records[0]["date"] == "2026-06-05"
    assert records[0]["realized_return_pct"] == 10.0
    assert records[0]["hold_duration_minutes"] == 40


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_bars(data_dir: Path, symbol: str, closes: list[float]) -> None:
    path = data_dir / "historical_bars" / f"{symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,close", f"2026-06-07,100,{closes[0]}"]
    for idx, close in enumerate(closes[1:], start=8):
        lines.append(f"2026-06-{idx:02d},100,{close}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_historical_research_inputs(data_dir: Path) -> None:
    _write_json(
        data_dir / "dynamic_scan_history" / "20260607T120000000000Z_live_bot.json",
        {
            "user_id": "live_bot",
            "candidates": [
                {
                    "symbol": "AAPL",
                    "source": "dynamic_universe",
                    "accepted": True,
                    "news_score": 8,
                    "event_score": 7,
                    "catalyst_score": 0.9,
                    "price": 101,
                    "rejection_reason": None,
                },
                {
                    "symbol": "MSFT",
                    "source": "news_catalyst",
                    "accepted": False,
                    "news_score": 9,
                    "event_score": 8,
                    "catalyst_score": 0.95,
                    "price": 50,
                    "rejection_reason": "below_min_relative_volume",
                },
                {
                    "symbol": "LOSS",
                    "source": "dynamic_universe",
                    "accepted": True,
                    "news_score": 6,
                    "event_score": 5,
                    "catalyst_score": 0.65,
                    "price": 20,
                },
            ],
        },
    )
    _write_json(
        data_dir / "trade_attribution" / "daily" / "2026-06-07_live_bot.json",
        {
            "date": "2026-06-07",
            "user_id": "live_bot",
            "orders": [
                {"symbol": "AAPL", "side": "buy", "submitted": True, "filled_avg_price": 102},
                {"symbol": "LOSS", "action": "buy", "submitted": True, "filled_avg_price": 21},
            ],
            "exits": [
                {"symbol": "AAPL", "entry_price": 102, "exit_price": 108, "pnl": 60},
                {"symbol": "LOSS", "entry_price": 21, "exit_price": 18, "pnl": -30},
            ],
        },
    )
    _write_bars(data_dir, "AAPL", [104, 106, 107, 108, 109, 110, 111, 112, 113, 114])
    _write_bars(data_dir, "MSFT", [105, 110, 112, 113, 115, 116, 117, 118, 119, 120])
    _write_bars(data_dir, "LOSS", [95, 93, 92, 91, 90, 89, 88, 87, 86, 85])


def test_historical_catalyst_outcome_database_enriches_candidates(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_historical_research_inputs(data_dir)

    rows = build_historical_catalyst_outcomes(data_dir=data_dir, user_id="live_bot", day="2026-06-07")
    by_symbol = {row["symbol"]: row for row in rows}

    assert set(by_symbol) == {"AAPL", "MSFT", "LOSS"}
    assert by_symbol["AAPL"]["bot_bought"] is True
    assert by_symbol["AAPL"]["entry_price"] == 102
    assert by_symbol["AAPL"]["exit_price"] == 108
    assert by_symbol["AAPL"]["realized_pnl"] == 60
    assert by_symbol["AAPL"]["1d_return"] == 4.0
    assert by_symbol["AAPL"]["3d_return"] == 7.0
    assert by_symbol["MSFT"]["bot_bought"] is False
    assert by_symbol["MSFT"]["rejection_reason"] == "below_min_relative_volume"
    assert by_symbol["MSFT"]["10d_return"] == 20.0


def test_historical_catalyst_summary_and_persistence(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_historical_research_inputs(data_dir)

    json_path, summary_path, text = write_historical_catalyst_outcome_report(
        data_dir=data_dir,
        user_id="live_bot",
        day="2026-06-07",
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    summary = summarize_historical_catalyst_outcomes(payload["records"])

    assert json_path == historical_catalyst_outcome_path(data_dir=data_dir, user_id="live_bot", day="2026-06-07")
    assert summary_path.exists()
    assert "Bought vs missed: candidates=3 bought=2 missed=1" in text
    assert "Best missed winners:" in text
    assert "MSFT" in text
    assert summary["bought_count"] == 2
    assert summary["missed_count"] == 1
    assert summary["avg_return_by_catalyst_score_bucket"][">=0.80"]["count"] == 2
    assert format_historical_catalyst_summary(day="2026-06-07", user_id="live_bot", rows=payload["records"])


def test_catalyst_outcomes_cli_latest_writes_research_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_historical_research_inputs(data_dir)

    assert latest_historical_catalyst_date(data_dir=data_dir, user_id="live_bot") == "2026-06-07"
    proc = subprocess.run(
        [
            str(PROJECT_ROOT / "bin" / "algo"),
            "catalyst-outcomes",
            "--date",
            "latest",
            "--user",
            "live_bot",
            "--data-dir",
            str(data_dir),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Historical Catalyst Outcomes 2026-06-07 [live_bot]" in proc.stdout
    assert (data_dir / "research" / "catalyst_outcomes" / "2026-06-07_live_bot.json").exists()
