from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from src.signal_expectancy_report import (
    build_signal_expectancy_report,
    render_signal_expectancy_report,
    write_signal_expectancy_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_bars(path: Path, *, base: float, up: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mults = [1.0, 1.01, 1.03, 1.05, 1.08, 1.10, 1.12] if up else [1.0, 0.99, 0.98, 0.96, 0.95, 0.94, 0.93]
    minutes = [0, 1, 5, 10, 15, 30, 60]
    lines = ["timestamp,open,high,low,close,volume"]
    for minute, mult in zip(minutes, mults):
        close = base * mult
        high = close * 1.01
        low = close * 0.99
        lines.append(f"2026-07-02T10:{minute:02d}:00-04:00,{close},{high},{low},{close},100000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_minute_bars(path: Path, *, day: str, base: float, minutes: int, step: float = 0.01) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["timestamp,open,high,low,close,volume"]
    start = pd.Timestamp(f"{day} 10:00:00", tz="America/New_York")
    for idx in range(minutes + 1):
        close = base + (idx * step)
        ts = (start + pd.Timedelta(minutes=idx)).isoformat()
        lines.append(f"{ts},{close},{close + 0.05},{close - 0.05},{close},100000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_backfill_bars_without_timestamp(path: Path, *, base: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["open,high,low,close,volume"]
    for idx in range(61):
        close = base * (1.0 + (idx * 0.001))
        lines.append(f"{close},{close * 1.01},{close * 0.99},{close},100000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_attribution(data_dir: Path) -> None:
    path = data_dir / "trade_attribution" / "daily" / "2026-07-02_live_bot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "date": "2026-07-02",
        "user_id": "live_bot",
        "candidates": [
            {
                "timestamp": "2026-07-02T10:00:00-04:00",
                "symbol": "SPY",
                "route": "trend_long",
                "price": 100.0,
                "relative_volume": 1.2,
                "gain_pct": 2.5,
                "market_regime": "bullish",
            },
            {
                "timestamp": "2026-07-02T10:00:00-04:00",
                "symbol": "RIVN",
                "route": "dynamic_momentum_override",
                "price": 20.0,
                "relative_volume": 0.7,
                "gain_pct": 12.0,
                "news_score": 0,
                "catalyst_score": 0,
                "market_regime": "neutral",
            },
        ],
        "allocator_candidates": [],
        "orders": [
            {
                "timestamp": "2026-07-02T10:00:00-04:00",
                "symbol": "SPY",
                "action": "buy",
                "route": "trend_long",
                "submitted": True,
                "filled_avg_price": 100.0,
                "filled_qty": 10,
                "relative_volume": 1.2,
                "gain_pct": 2.5,
            },
            {
                "timestamp": "2026-07-02T10:00:00-04:00",
                "symbol": "RIVN",
                "action": "buy",
                "route": "dynamic_momentum_override",
                "submitted": False,
                "reject_reason": "weak_catalyst_dynamic_non_exceptional_live",
                "filled_avg_price": None,
                "price": 20.0,
                "relative_volume": 0.7,
                "gain_pct": 12.0,
            },
        ],
        "exits": [
            {
                "timestamp": "2026-07-02T10:30:00-04:00",
                "symbol": "SPY",
                "exit_reason": "profit_target",
                "pnl": 80.0,
                "pnl_pct": 8.0,
                "hold_minutes": 30,
                "entry_route": "trend_long",
            }
        ],
        "summary": {},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_custom_attribution(data_dir: Path, *, day: str, candidates: list[dict]) -> None:
    path = data_dir / "trade_attribution" / "daily" / f"{day}_live_bot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "date": day,
                "user_id": "live_bot",
                "candidates": candidates,
                "allocator_candidates": [],
                "orders": [],
                "exits": [],
                "summary": {},
            }
        ),
        encoding="utf-8",
    )


def _sample_log() -> str:
    return "\n".join(
        [
            "2026-07-02T10:00:00-04:00 INFO ENTRY_EVAL_PASS symbol=SPY route=trend_long price=100 relative_volume=1.2 gain_pct=2.5",
            "2026-07-02T10:00:00-04:00 INFO DYNAMIC_SCAN RIVN: price=20 gain=12 vol=1000000 avg=100000 rel=10 spread=0.1% vwap_above=True news_score=0 catalyst_score=0",
            "2026-07-02T10:00:01-04:00 INFO ORDER_SKIP symbol=RIVN reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator price=20 relative_volume=0.7 gain_pct=12",
        ]
    )


def test_signal_expectancy_route_symbol_and_time_tables(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_attribution(data_dir)
    _write_bars(data_dir / "historical_bars" / "SPY_2026-07-02_1Min.csv", base=100, up=True)
    _write_bars(data_dir / "historical_bars" / "RIVN_2026-07-02_1Min.csv", base=20, up=False)

    report = build_signal_expectancy_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    routes = {row["route"]: row for row in report["route_expectancy"]}
    assert routes["trend_long"]["trades"] >= 1
    assert routes["trend_long"]["realized_pnl"] == pytest.approx(80.0)
    assert routes["trend_long"]["win_rate"] == pytest.approx(1.0)
    assert routes["trend_long"]["profit_factor"] == pytest.approx(80.0)
    assert routes["trend_long"]["avg_15m_return"] == pytest.approx(8.0)
    assert routes["dynamic_momentum_override"]["skipped"] >= 1
    assert routes["dynamic_momentum_override"]["avg_15m_return"] == pytest.approx(-5.0)

    symbols = {(row["symbol"], row["route"]): row for row in report["symbol_expectancy"]}
    assert symbols[("SPY", "trend_long")]["recommendation"] == "increase_review"
    assert symbols[("RIVN", "dynamic_momentum_override")]["recommendation"] == "reduce_review"

    buckets = {row["time_bucket"]: row for row in report["time_of_day_expectancy"]}
    assert buckets["10:00-11:00"]["count"] >= 2
    assert report["executive_summary"]["best_route_by_expectancy"]["route"] == "trend_long"
    assert report["data_quality"]["signals_analyzed"] >= 4
    assert report["data_quality"]["signals_with_valid_forward_bars"] > 0
    assert report["data_quality"]["lookup_success_rate"] is not None
    assert report["data_quality"]["cache_hits"] > 0
    assert report["data_quality"]["lookup_failure_breakdown"] == {}
    assert report["executive_summary"]["recommendation_status"] == "BLOCKED_DATA_QUALITY"
    assert "valid_sample_quality_below_threshold" in report["executive_summary"]["recommendation_blocking_reasons"]
    assert report["suggested_config"]["suggested_config"] == {}


def test_signal_expectancy_render_and_groups(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_attribution(data_dir)
    _write_bars(data_dir / "historical_bars" / "SPY_2026-07-02_1Min.csv", base=100, up=True)
    _write_bars(data_dir / "historical_bars" / "RIVN_2026-07-02_1Min.csv", base=20, up=False)

    report = build_signal_expectancy_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    text = render_signal_expectancy_report(report)
    assert "Signal Expectancy Report 2026-07-02 user=live_bot" in text
    assert "| trend_long |" in text
    assert "| RIVN | dynamic_momentum_override |" in text
    assert "## Data Quality" in text
    assert "signals with valid forward bars" in text
    assert "lookup failure breakdown" in text
    assert "## Suggested Config" in text
    assert "candidate reduced_symbols" in text
    assert "recommendation_status: BLOCKED_DATA_QUALITY" in text
    assert report["groups"]["catalyst_strength"][0]["catalyst_strength"] in {"none", "missing"}
    assert report["groups"]["rvol_bucket"]
    assert report["groups"]["gain_bucket"]
    assert report["groups"]["market_regime"]


def test_signal_expectancy_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_attribution(data_dir)
    _write_bars(data_dir / "historical_bars" / "SPY_2026-07-02_1Min.csv", base=100, up=True)
    log_dir = data_dir / "review" / "2026-07-02"
    log_dir.mkdir(parents=True)
    (log_dir / "live.log").write_text(_sample_log(), encoding="utf-8")

    json_path, text_path, report = write_signal_expectancy_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
    )

    assert json_path == data_dir / "research_metrics" / "2026-07-02" / "signal_expectancy_report.json"
    assert text_path == data_dir / "research_metrics" / "2026-07-02" / "signal_expectancy_report.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["report"] == "signal_expectancy_report"
    assert "Signal Expectancy Report 2026-07-02 user=live_bot" in text_path.read_text(encoding="utf-8")
    assert report["data_quality"]["log_lines_read"] == 3

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_signal_expectancy_report.py"),
            "--date",
            "2026-07-02",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(data_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Signal Expectancy Report 2026-07-02 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout


def test_signal_expectancy_loads_timestampless_historical_backfill_bars(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_attribution(data_dir)
    _write_backfill_bars_without_timestamp(data_dir / "historical_bars" / "SPY_2026-07-02_1Min.csv", base=100)

    report = build_signal_expectancy_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
        log_text="",
    )

    routes = {row["route"]: row for row in report["route_expectancy"]}
    quality = report["data_quality"]
    assert routes["trend_long"]["avg_15m_return"] == pytest.approx(4.5)
    assert "SPY" in quality["symbols_with_bars"]
    assert any(path.endswith("SPY_2026-07-02_1Min.csv") for path in quality["bars_files_found"])
    assert "SPY" not in quality["missing_symbols"]


def test_signal_expectancy_prefers_exact_symbol_bar_file_over_prefix_collision(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_custom_attribution(
        data_dir,
        day="2026-07-28",
        candidates=[
            {
                "timestamp": "2026-07-28T10:00:00-04:00",
                "symbol": "MU",
                "route": "momentum_breakout",
                "price": 80.0,
            }
        ],
    )
    _write_minute_bars(data_dir / "historical_bars" / "MUZ_2026-07-28_1Min.csv", day="2026-07-28", base=800.0, minutes=60, step=1.0)
    _write_minute_bars(data_dir / "historical_bars" / "MU_2026-07-28_1Min.csv", day="2026-07-28", base=80.0, minutes=60, step=0.10)

    report = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day="2026-07-28", user_id="live_bot", log_text="")

    signal = report["signals"][0]
    quality = report["data_quality"]
    assert signal["forward_lookup_status"] == "available"
    assert signal["return_15m_pct"] == pytest.approx(1.875)
    assert signal["max_favorable_excursion_pct"] == pytest.approx(7.5625)
    assert signal["forward_lookup_source"].endswith("MU_2026-07-28_1Min.csv")
    assert not signal["forward_lookup_source"].endswith("MUZ_2026-07-28_1Min.csv")
    assert quality["anomalous_observations"] == 0


def test_signal_expectancy_excludes_extreme_uncorroborated_returns_and_blocks_recommendations(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_custom_attribution(
        data_dir,
        day="2026-07-28",
        candidates=[
            {
                "timestamp": "2026-07-28T10:00:00-04:00",
                "symbol": "MU",
                "route": "momentum_breakout",
                "price": 16.0,
            }
        ],
    )
    _write_minute_bars(data_dir / "historical_bars" / "MU_2026-07-28_1Min.csv", day="2026-07-28", base=800.0, minutes=60, step=1.0)

    report = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day="2026-07-28", user_id="live_bot", log_text="")

    quality = report["data_quality"]
    signal = report["signals"][0]
    assert quality["signals_with_valid_forward_bars"] == 0
    assert quality["missing_bars"] == 1
    assert quality["anomaly_breakdown"]["EXTREME_RETURN_UNCORROBORATED"] >= 1
    assert signal["forward_lookup_failure_reason"] == "extreme_return_uncorroborated"
    assert signal["return_15m_pct"] is None
    assert report["executive_summary"]["recommendation_status"] == "BLOCKED_DATA_QUALITY"
    assert report["executive_summary"]["routes_to_increase"] == []


def test_signal_expectancy_requires_primary_forward_horizon_in_partial_file(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_custom_attribution(
        data_dir,
        day="2026-07-28",
        candidates=[{"timestamp": "2026-07-28T10:00:00-04:00", "symbol": "SPY", "route": "trend_long", "price": 100.0}],
    )
    _write_minute_bars(data_dir / "historical_bars" / "SPY_2026-07-28_1Min.csv", day="2026-07-28", base=100.0, minutes=10, step=0.10)

    report = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day="2026-07-28", user_id="live_bot", log_text="")

    assert report["data_quality"]["signals_with_valid_forward_bars"] == 0
    assert report["data_quality"]["missing_bars"] == 1
    assert report["signals"][0]["forward_window_15m_valid"] is False

    _write_minute_bars(data_dir / "historical_bars" / "SPY_2026-07-28_1Min.csv", day="2026-07-28", base=100.0, minutes=15, step=0.10)
    report = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day="2026-07-28", user_id="live_bot", log_text="")

    assert report["data_quality"]["signals_with_valid_forward_bars"] == 1
    assert report["data_quality"]["missing_bars"] == 0
    assert report["signals"][0]["forward_window_15m_valid"] is True


def test_signal_expectancy_classifies_missing_local_bar_source(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "historical_bars").mkdir(parents=True)
    _write_custom_attribution(
        data_dir,
        day="2026-07-23",
        candidates=[
            {
                "timestamp": "2026-07-23T10:00:00-04:00",
                "symbol": "SPY",
                "route": "trend_long",
                "price": 100.0,
            }
        ],
    )

    report = build_signal_expectancy_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-23",
        user_id="live_bot",
        log_text="",
    )

    quality = report["data_quality"]
    assert quality["signals_with_valid_forward_bars"] == 0
    assert quality["missing_bars"] == 1
    assert quality["lookup_failure_breakdown"] == {"no_historical_source": 1}
    assert quality["symbols_missing_bars_by_reason"]["no_historical_source"] == ["SPY"]
    assert quality["persistence_status"] == {"no_local_bar_file_for_symbol_day": 1}
    assert report["signals"][0]["forward_lookup_unavailable_code"] == "OUTCOME_UNAVAILABLE_NO_HISTORICAL_SOURCE"


@pytest.mark.parametrize(
    ("day", "timestamp", "reason"),
    [
        ("2026-07-25", "2026-07-25T10:00:00-04:00", "weekend"),
        ("2026-07-03", "2026-07-03T10:00:00-04:00", "market_holiday"),
        ("2026-07-23", "2026-07-23T21:00:00-04:00", "timestamp_outside_session"),
        ("2026-07-23", "2026-07-24T00:30:00+00:00", "timestamp_outside_session"),
    ],
)
def test_signal_expectancy_classifies_session_lookup_failures(tmp_path: Path, day: str, timestamp: str, reason: str) -> None:
    data_dir = tmp_path / "data"
    _write_custom_attribution(
        data_dir,
        day=day,
        candidates=[{"timestamp": timestamp, "symbol": "SPY", "route": "trend_long", "price": 100.0}],
    )
    _write_bars(data_dir / "historical_bars" / f"SPY_{day}_1Min.csv", base=100, up=True)

    report = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day=day, user_id="live_bot", log_text="")

    assert report["data_quality"]["lookup_failure_breakdown"] == {reason: 1}
    assert report["signals"][0]["forward_lookup_failure_reason"] == reason


def test_signal_expectancy_classifies_invalid_symbol_and_replay_records(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_custom_attribution(
        data_dir,
        day="2026-07-23",
        candidates=[
            {"timestamp": "2026-07-23T10:00:00-04:00", "symbol": "BAD SYMBOL", "route": "trend_long", "price": 100.0},
            {
                "timestamp": "2026-07-23T10:00:00-04:00",
                "symbol": "SPY",
                "route": "trend_long",
                "price": 100.0,
                "broker_order_id": "replay-1",
            },
        ],
    )

    report = build_signal_expectancy_report(project_root=tmp_path, data_dir=data_dir, day="2026-07-23", user_id="live_bot", log_text="")

    assert report["data_quality"]["signals_analyzed"] == 1
    assert report["data_quality"]["lookup_failure_breakdown"] == {"invalid_symbol": 1}
    assert report["signals"][0]["symbol"] == "BAD SYMBOL"
    assert report["signals"][0]["forward_lookup_unavailable_code"] == "OUTCOME_UNAVAILABLE_INVALID_SYMBOL"


def test_signal_expectancy_excludes_replay_labelled_log_rows_for_live_user(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_custom_attribution(data_dir, day="2026-07-23", candidates=[])

    report = build_signal_expectancy_report(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-23",
        user_id="live_bot",
        log_text="\n".join(
            [
                "2026-07-23T10:00:00-04:00 INFO ENTRY_EVAL_PASS symbol=SPY route=premarket_catalyst_replay price=100",
                "2026-07-23T10:01:00-04:00 INFO ENTRY_EVAL_PASS symbol=QQQ route=trend_long price=100",
            ]
        ),
    )

    assert report["data_quality"]["signals_analyzed"] == 1
    assert report["signals"][0]["symbol"] == "QQQ"
