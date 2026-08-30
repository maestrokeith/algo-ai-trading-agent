from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import src.dynamic_weak_catalyst_outcomes as dwco
from src.dynamic_weak_catalyst_outcomes import (
    build_dynamic_weak_catalyst_outcomes,
    render_dynamic_weak_catalyst_outcomes,
    write_dynamic_weak_catalyst_outcomes,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_bars(path: Path, symbol: str, *, base: float = 10.0, day: str = "2026-07-01") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        (f"{day}T10:00:00-04:00", base, base * 1.00, base * 0.99),
        (f"{day}T10:01:00-04:00", base * 1.05, base * 1.06, base * 0.98),
        (f"{day}T10:05:00-04:00", base * 1.10, base * 1.12, base * 0.97),
        (f"{day}T10:10:00-04:00", base * 1.00, base * 1.14, base * 0.95),
        (f"{day}T10:15:00-04:00", base * 1.20, base * 1.22, base * 1.00),
        (f"{day}T10:30:00-04:00", base * 1.15, base * 1.25, base * 1.10),
        (f"{day}T11:00:00-04:00", base * 1.25, base * 1.26, base * 1.12),
    ]
    lines = ["timestamp,open,high,low,close,volume"]
    for ts, close, high, low in rows:
        lines.append(f"{ts},{close},{high},{low},{close},100000")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sample_log() -> str:
    return "\n".join(
        [
            "2026-07-01T10:00:00-04:00 INFO ALLOCATOR ACTIONS: "
            "[{'action': 'buy', 'symbol': 'ABCD', 'route': 'dynamic_momentum_override', "
            "'paper_current_price': 10.0, 'relative_volume': 0.65, 'gain_pct': 11.0, "
            "'catalyst_age_minutes': 45.0, 'market_regime': 'bullish'}]",
            "2026-07-01T10:00:00-04:00 INFO ORDER_SKIP symbol=ABCD reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
            "2026-07-01T10:00:00-04:00 INFO ALLOCATOR ACTIONS: "
            "[{'action': 'buy', 'symbol': 'WXYZ', 'route': 'dynamic_momentum_override', "
            "'paper_current_price': 20.0, 'relative_volume': 1.35, 'gain_pct': 22.0, "
            "'catalyst_age_minutes': 10.0, 'market_regime': 'neutral'}]",
            "2026-07-01T10:00:00-04:00 INFO ORDER_SKIP symbol=WXYZ reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
        ]
    )


def test_weak_catalyst_outcomes_parse_skips_and_forward_returns(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_bars(data_dir / "historical_bars" / "ABCD_2026-07-01_1Min.csv", "ABCD", base=10.0)
    _write_bars(data_dir / "historical_bars" / "WXYZ_2026-07-01_1Min.csv", "WXYZ", base=20.0)

    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
    )

    assert report["research_only"] is True
    assert report["summary"]["skips"] == 2
    assert report["summary"]["forward_returns_available"] == 2
    assert report["summary"]["returns"]["1m"]["average_return_pct"] == pytest.approx(5.0)
    assert report["summary"]["returns"]["15m"]["median_return_pct"] == pytest.approx(20.0)
    assert report["summary"]["returns"]["15m"]["win_rate"] == pytest.approx(1.0)
    assert report["summary"]["returns"]["max_drawdown_pct"] == pytest.approx(-5.0)
    assert report["summary"]["returns"]["max_excursion_pct"] == pytest.approx(26.0)
    rows = {row["symbol"]: row for row in report["events"]}
    assert rows["ABCD"]["entry_price"] == pytest.approx(10.0)
    assert rows["ABCD"]["return_5m_pct"] == pytest.approx(10.0)
    assert rows["ABCD"]["rvol_bucket"] == "0.5-0.8"
    assert rows["ABCD"]["gain_bucket"] == "10-20%"
    assert rows["ABCD"]["catalyst_age_bucket"] == "30-120m"
    assert rows["WXYZ"]["rvol_bucket"] == ">1.2"
    assert report["splits"]["market_regime"]["bullish"]["count"] == 1
    assert report["summary"]["debug_counts"]["matched_allocator_context"] == 2
    assert report["summary"]["debug_counts"]["matched_scan_context"] == 0
    assert report["summary"]["debug_counts"]["missing_entry"] == 0


def test_weak_catalyst_outcomes_reports_live_exception_eligibility(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_bars(data_dir / "historical_bars" / "RIVN_2026-07-01_1Min.csv", "RIVN", base=10.0)
    _write_bars(data_dir / "historical_bars" / "LOWQ_2026-07-01_1Min.csv", "LOWQ", base=10.0)
    logs = "\n".join(
        [
            "2026-07-01T10:00:00-04:00 INFO ALLOCATOR ACTIONS: "
            "[{'action': 'buy', 'symbol': 'RIVN', 'route': 'dynamic_momentum_override', "
            "'paper_current_price': 10.0, 'relative_volume': 0.65, 'gain_pct': 12.0, "
            "'spread_pct': 0.2, 'atr_pct': 5.0, 'entry_eval_final': True}]",
            "2026-07-01T10:00:00-04:00 INFO ORDER_SKIP symbol=RIVN reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
            "2026-07-01T10:01:00-04:00 INFO ALLOCATOR ACTIONS: "
            "[{'action': 'buy', 'symbol': 'LOWQ', 'route': 'dynamic_momentum_override', "
            "'paper_current_price': 10.0, 'relative_volume': 0.65, 'gain_pct': 8.0, "
            "'spread_pct': 0.2, 'atr_pct': 5.0, 'entry_eval_final': True}]",
            "2026-07-01T10:01:00-04:00 INFO ORDER_SKIP symbol=LOWQ reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
        ]
    )
    config = {
        "dynamic_universe": {
            "live_weak_catalyst_exception_experiment": {
                "enabled": True,
                "min_price": 8,
                "min_gain_pct": 10,
                "min_relative_volume": 0.5,
                "max_spread_pct": 0.25,
                "require_entry_eval_pass": True,
                "max_atr_pct": 15,
                "max_positions_per_day": 1,
                "notional_cap": 300,
                "require_no_existing_position": True,
            }
        }
    }

    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-01",
        user_id="live_bot",
        log_text=logs,
        config=config,
    )

    rows = {row["symbol"]: row for row in report["events"]}
    assert rows["RIVN"]["live_weak_catalyst_exception_qualifies"] is True
    assert rows["RIVN"]["live_weak_catalyst_exception_reason"] == "qualified"
    assert rows["RIVN"]["live_weak_catalyst_exception_notional_cap"] == pytest.approx(300.0)
    assert rows["LOWQ"]["live_weak_catalyst_exception_qualifies"] is False
    assert rows["LOWQ"]["live_weak_catalyst_exception_reason"] == "gain_below_min"
    rendered = render_dynamic_weak_catalyst_outcomes(report)
    assert "exception_qualifies=True exception_reason=qualified" in rendered
    assert "exception_qualifies=False exception_reason=gain_below_min" in rendered


def test_weak_catalyst_outcomes_falls_back_to_attribution_and_bar_entry_price(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_bars(data_dir / "historical_bars" / "MISS_2026-07-01_1Min.csv", "MISS", base=10.0)
    path = data_dir / "trade_attribution" / "daily" / "2026-07-01_live_bot.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "symbol": "MISS",
                        "timestamp": "2026-07-01T10:00:00-04:00",
                        "reject_reason": "weak_catalyst_dynamic_non_exceptional_live",
                        "relative_volume": 0.4,
                        "gain_pct": 4.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-01",
        user_id="live_bot",
        log_text="",
    )

    row = report["events"][0]
    assert row["symbol"] == "MISS"
    assert row["entry_price_source"] == "bars"
    assert row["entry_price"] == pytest.approx(10.0)
    assert row["rvol_bucket"] == "0.3-0.5"
    assert row["gain_bucket"] == "<5%"


def test_weak_catalyst_outcomes_matches_nearest_allocator_context_for_rivn(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_bars(data_dir / "historical_bars" / "RIVN_2026-07-01_1Min.csv", "RIVN", base=20.0)
    logs = "\n".join(
        [
            "2026-07-01T10:08:00-04:00 INFO ALLOCATOR ACTIONS: "
            "[{'action': 'buy', 'symbol': 'RIVN', 'paper_current_price': 19.0, "
            "'relative_volume': 0.31, 'gain_pct': 7.0, 'catalyst_age_minutes': 99.0, "
            "'market_regime': 'old'}]",
            "2026-07-01T10:09:00-04:00 INFO ALLOCATOR ACTIONS: "
            "[{'action': 'buy', 'symbol': 'RIVN', 'paper_current_price': 20.0, "
            "'relative_volume': 0.6879, 'gain_pct': 13.2284, 'catalyst_age_minutes': 2.75, "
            "'market_regime': 'bullish'}]",
            "2026-07-01T10:09:16-04:00 INFO ORDER_SKIP symbol=RIVN reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
            "2026-07-01T10:10:00-04:00 INFO ALLOCATOR ACTIONS: "
            "[{'action': 'buy', 'symbol': 'RIVN', 'paper_current_price': 25.0, "
            "'relative_volume': 1.50, 'gain_pct': 30.0, 'catalyst_age_minutes': 1.0, "
            "'market_regime': 'future'}]",
        ]
    )

    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-01",
        user_id="live_bot",
        log_text=logs,
    )

    row = report["events"][0]
    assert row["symbol"] == "RIVN"
    assert row["entry_price"] == pytest.approx(20.0)
    assert row["relative_volume"] == pytest.approx(0.6879)
    assert row["gain_pct"] == pytest.approx(13.2284)
    assert row["catalyst_age_minutes"] == pytest.approx(2.75)
    assert row["market_regime"] == "bullish"
    assert row["matched_allocator_context"] is True
    assert row["rvol_bucket"] == "0.5-0.8"
    assert report["summary"]["debug_counts"]["matched_allocator_context"] == 1


def test_weak_catalyst_outcomes_parses_exact_rivn_journal_allocator_actions(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_bars(
        data_dir / "historical_bars" / "RIVN_2026-07-02_1Min.csv",
        "RIVN",
        base=19.475,
        day="2026-07-02",
    )
    allocator_line = (
        "Jul 02 10:09:16 algosphere-live-host python3.12[1789802]: ALLOCATOR ACTIONS: "
        "[{'action': 'buy', 'symbol': 'RIVN', 'notional': 1200.0, 'source': 'dynamic_universe', "
        "'route': 'dynamic_momentum_override', 'dynamic_candidate': True, "
        "'signal_score': 0.15811770721984608, 'relative_volume': 0.6879766184064555, "
        "'rel_volume': 0.6918510193813463, 'effective_min_rel_volume': 0.3, "
        "'scanner_effective_min_rel_volume': 0.3, 'entry_effective_min_rel_volume': 0.3, "
        "'entry_eval_effective_min_rel_volume': 0.3, 'catalyst_fastlane_active': False, "
        "'catalyst_min_relative_volume': 0.35, 'gain_pct': 13.228438228438222, "
        "'day_gain_pct': 13.228438228438222, 'dynamic_score': 0.15811770721984608, "
        "'scanner_score': 0.15811770721984608, 'is_dynamic': True, 'dynamic_symbol': True, "
        "'weak_catalyst_dynamic': True, 'news_score': 0.0, 'catalyst_score': 0.0, "
        "'event_score': 0.0, 'article_count': 0.0, 'catalyst_age_minutes': 2.7504884, "
        "'premarket_injected': False, 'entry_eval_final': True, 'scanner_price_above_vwap': True, "
        "'paper_current_price': 19.475, 'paper_session_vwap': 18.90212025118968, "
        "'scanner_relative_volume': 0.6879766184064555, 'entry_relative_volume': 0.6879766184064555, "
        "'allocator_relative_volume': 0.6879766184064555}]"
    )
    logs = "\n".join(
        [
            allocator_line,
            "Jul 02 10:09:16 algosphere-live-host python3.12[1789802]: ORDER_SKIP symbol=RIVN reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
            "Jul 02 10:09:16 algosphere-live-host python3.12[1789802]: ALLOCATOR_DISPATCH_SKIPPED symbol=RIVN reason=weak_catalyst_dynamic_non_exceptional_live",
        ]
    )

    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
        log_text=logs,
    )

    row = report["events"][0]
    assert row["symbol"] == "RIVN"
    assert row["entry_time"] == "2026-07-02T10:09:16-04:00"
    assert row["entry_price"] == pytest.approx(19.475)
    assert row["relative_volume"] == pytest.approx(0.688)
    assert row["gain_pct"] == pytest.approx(13.2284)
    assert row["catalyst_age_minutes"] == pytest.approx(2.7505)
    assert row["matched_allocator_context"] is True
    assert report["summary"]["debug_counts"]["matched_allocator_context"] == 1
    assert report["summary"]["debug_counts"]["missing_entry"] == 0
    debug = report["debug"]
    assert debug["parsed_allocator_actions"][0]["timestamp"] == "2026-07-02T10:09:16-04:00"
    assert debug["parsed_allocator_actions"][0]["symbol"] == "RIVN"
    assert debug["parsed_order_skips"][0]["matched_allocator_context"] is True


def test_weak_catalyst_outcomes_falls_back_to_journalctl_when_local_file_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    review_dir = data_dir / "review" / "2026-07-02"
    review_dir.mkdir(parents=True)
    stale_file = review_dir / "live.log"
    stale_file.write_text("\n".join(f"stale local line {idx}" for idx in range(18)) + "\n", encoding="utf-8")
    _write_bars(
        data_dir / "historical_bars" / "RIVN_2026-07-02_1Min.csv",
        "RIVN",
        base=19.475,
        day="2026-07-02",
    )
    journal_lines = [
        "Jul 02 10:09:16 algosphere-live-host python3.12[1789802]: ALLOCATOR ACTIONS: "
        "[{'action': 'buy', 'symbol': 'RIVN', 'paper_current_price': 19.475, "
        "'relative_volume': 0.6879766184064555, 'gain_pct': 13.228438228438222, "
        "'catalyst_age_minutes': 2.7504884, 'scanner_relative_volume': 0.6879766184064555}]",
        "Jul 02 10:09:16 algosphere-live-host python3.12[1789802]: DYNAMIC_SELECTED symbol=RIVN score=0.15 news_score=0",
        "Jul 02 10:09:16 algosphere-live-host python3.12[1789802]: ORDER_SKIP symbol=RIVN reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
    ]

    class Proc:
        returncode = 0
        stdout = "\n".join(journal_lines) + "\n"

    captured: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> Proc:
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(dwco.subprocess, "run", fake_run)

    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-02",
        user_id="live_bot",
    )

    assert captured["cmd"] == [
        "journalctl",
        "-u",
        "algo.service",
        "--since",
        "2026-07-02 00:00:00",
        "--until",
        "2026-07-02 23:59:59",
        "--no-pager",
    ]
    debug = report["debug"]
    assert debug["LOG_SOURCE"] == "journalctl"
    assert debug["used_journalctl"] is True
    assert debug["local_lines"] == 18
    assert debug["journal_lines_total"] == 3
    assert debug["grep_counts"]["ALLOCATOR ACTIONS"] == 1
    assert debug["grep_counts"]["ORDER_SKIP weak_catalyst_dynamic_non_exceptional_live"] == 1
    assert debug["grep_counts"]["DYNAMIC_SELECTED"] == 1
    row = report["events"][0]
    assert row["symbol"] == "RIVN"
    assert row["entry_price"] == pytest.approx(19.475)
    assert row["matched_allocator_context"] is True
    assert report["summary"]["debug_counts"]["matched_allocator_context"] == 1


def test_weak_catalyst_outcomes_uses_scan_context_price_for_arct_sparse_skip(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_bars(data_dir / "historical_bars" / "ARCT_2026-07-01_1Min.csv", "ARCT", base=8.5)
    logs = "\n".join(
        [
            "2026-07-01T10:00:00-04:00 INFO DYNAMIC_SELECTED symbol=ARCT score=46.49 "
            "price=8.50 relative_volume=0.55 gain_pct=18.2 age_minutes=6.0 "
            "market_regime=neutral news_score=0 event_score=0 catalyst_score=0",
            "2026-07-01T10:00:03-04:00 INFO ORDER_SKIP symbol=ARCT reason=weak_catalyst_dynamic_non_exceptional_live source=capital_allocator",
        ]
    )

    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-01",
        user_id="live_bot",
        log_text=logs,
    )

    row = report["events"][0]
    assert row["symbol"] == "ARCT"
    assert row["entry_price"] == pytest.approx(8.5)
    assert row["entry_price_source"] == "log"
    assert row["relative_volume"] == pytest.approx(0.55)
    assert row["gain_pct"] == pytest.approx(18.2)
    assert row["catalyst_age_minutes"] == pytest.approx(6.0)
    assert row["market_regime"] == "neutral"
    assert row["matched_allocator_context"] is False
    assert row["matched_scan_context"] is True
    assert report["summary"]["debug_counts"]["matched_scan_context"] == 1
    assert report["summary"]["debug_counts"]["missing_entry"] == 0


def test_weak_catalyst_outcomes_debug_counts_missing_entry_and_bars(tmp_path: Path) -> None:
    report = build_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text="2026-07-01T10:00:00-04:00 INFO ORDER_SKIP symbol=NONE reason=weak_catalyst_dynamic_non_exceptional_live",
    )

    debug = report["summary"]["debug_counts"]
    assert debug["missing_entry"] == 1
    assert debug["missing_forward_bars"] == 1
    assert report["summary"]["missing_forward_returns"] == 1


def test_weak_catalyst_outcomes_writes_artifacts_and_cli(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _write_bars(data_dir / "historical_bars" / "ABCD_2026-07-01_1Min.csv", "ABCD", base=10.0)
    log_file = tmp_path / "weak.log"
    log_file.write_text(_sample_log().splitlines()[0] + "\n" + _sample_log().splitlines()[1] + "\n", encoding="utf-8")

    json_path, text_path, report = write_dynamic_weak_catalyst_outcomes(
        project_root=tmp_path,
        data_dir=data_dir,
        day="2026-07-01",
        user_id="live_bot",
        log_files=[log_file],
    )

    assert json_path == data_dir / "research_metrics" / "2026-07-01" / "dynamic_weak_catalyst_outcomes.json"
    assert text_path == data_dir / "research_metrics" / "2026-07-01" / "dynamic_weak_catalyst_outcomes.md"
    assert json.loads(json_path.read_text(encoding="utf-8"))["report"] == "dynamic_weak_catalyst_outcomes"
    text = render_dynamic_weak_catalyst_outcomes(report)
    assert "Dynamic Weak Catalyst Outcomes 2026-07-01 user=live_bot" in text
    assert "| 15m | 1 | 20.0 | 20.0 | 1.0 |" in text

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_weak_catalyst_outcomes.py"),
            "--date",
            "2026-07-01",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(data_dir),
            "--log-file",
            str(log_file),
            "--debug",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        cwd=PROJECT_ROOT,
    )

    assert proc.returncode == 0, proc.stderr
    assert "DEBUG dynamic-weak-catalyst-outcomes" in proc.stdout
    assert "LOG_SOURCE files" in proc.stdout
    assert "used_journalctl=no" in proc.stdout
    assert "ALLOCATOR ACTIONS=1" in proc.stdout
    assert "ORDER_SKIP weak_catalyst_dynamic_non_exceptional_live=1" in proc.stdout
    assert "parsed_allocator_actions:" in proc.stdout
    assert "parsed_order_skips:" in proc.stdout
    assert "matched_allocator_context=True" in proc.stdout
    assert "JSON:" in proc.stdout
    assert "Markdown:" in proc.stdout
