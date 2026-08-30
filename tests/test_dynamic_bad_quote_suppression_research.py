from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_bad_quote_suppression_research import (
    build_dynamic_bad_quote_suppression_research,
    render_dynamic_bad_quote_suppression_research,
    write_dynamic_bad_quote_suppression_research,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return {"universe": {"symbols": ["SPY", "QQQ"]}, "execution": {"large_cap_symbols": ["AAPL"]}}


def _sample_log() -> str:
    return "\n".join(
        [
            "Jul 02 13:42:42 algosphere-live-host python3.12[1799031]: Unstable quote BMGL",
            "Jul 02 13:42:42 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject BMGL: bad quote price=0.0 bid=0.0 ask=0.0",
            "Jul 02 13:45:42 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN BMGL: price=0.11 gain=25.0 vol=100000 avg=20000 rel=5.0 spread=3.0% news_score=0 catalyst_score=0 event_score=0",
            "Jul 02 13:45:42 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject BMGL: bad quote price=0.0 bid=0.0 ask=0.0",
            "Jul 02 13:47:42 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject BMGL: bad quote price=0.0 bid=0.0 ask=0.0",
            "Jul 02 13:42:46 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject PLBL: bad quote price=10.3 bid=10.3 ask=0.0",
            "Jul 02 13:42:47 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject SPY: bad quote price=0.0 bid=0.0 ask=0.0",
            "Jul 02 13:42:48 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject SPY: bad quote price=0.0 bid=0.0 ask=0.0",
            "Jul 02 13:42:49 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject NEWS: bad quote price=20.0 bid=0.0 ask=0.0 news_score=8 catalyst_score=0.8 event_score=0",
            "Jul 02 13:43:49 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject NEWS: bad quote price=20.0 bid=0.0 ask=0.0 news_score=8 catalyst_score=0.8 event_score=0",
            "Jul 02 13:44:49 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject LATER: bad quote price=0.0 bid=0.0 ask=0.0",
            "Jul 02 13:45:49 algosphere-live-host python3.12[1799031]: DYNAMIC_SCAN reject LATER: bad quote price=0.0 bid=0.0 ask=0.0",
            "Jul 02 13:46:49 algosphere-live-host python3.12[1799031]: DYNAMIC_SELECTED symbol=LATER score=70",
            "Jul 02 13:47:49 algosphere-live-host python3.12[1799031]: ENTRY_EVAL_PASS symbol=LATER route=dynamic_momentum_override",
        ]
    )


def _write_history(path: Path) -> None:
    payload = {
        "generated_at": "2026-07-02T13:40:00-04:00",
        "user_id": "live_bot",
        "candidates": [
            {
                "symbol": "HIST",
                "timestamp": "2026-07-02T13:40:00-04:00",
                "accepted": False,
                "rejection_reason": "unstable quote",
                "price": 0.0,
                "bid": 0.0,
                "ask": 0.0,
                "volume": 50000,
                "relative_volume": 2.0,
                "gain_pct": 12.0,
                "news_score": 0,
                "catalyst_score": 0,
                "event_score": 0,
            },
            {
                "symbol": "HIST",
                "timestamp": "2026-07-02T13:41:00-04:00",
                "accepted": True,
                "price": 8.0,
                "bid": 7.99,
                "ask": 8.01,
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_bad_quote_suppression_research_aggregates_repeated_symbols(tmp_path: Path) -> None:
    report = build_dynamic_bad_quote_suppression_research(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-02",
        user_id="live_bot",
        log_text=_sample_log(),
        config=_config(),
    )

    rows = {row["symbol"]: row for row in report["symbols"]}
    assert rows["BMGL"]["count"] == 4
    assert rows["BMGL"]["bad_quote_count"] == 3
    assert rows["BMGL"]["unstable_quote_count"] == 1
    assert rows["BMGL"]["zero_bid_or_ask_count"] == 4
    assert rows["BMGL"]["ask_zero_count"] == 4
    assert rows["BMGL"]["price_zero_count"] == 3
    assert rows["BMGL"]["average_volume"] == 100000
    assert rows["BMGL"]["average_relative_volume"] == 5.0
    assert rows["BMGL"]["average_gain_pct"] == 25.0
    assert rows["BMGL"]["recommended_suppression"] == "suppress_rest_of_day_after_3_bad_quotes"
    assert rows["PLBL"]["ask_zero_count"] == 1
    assert report["summary"]["recommended_suppression_symbols"] == 1


def test_bad_quote_suppression_research_respects_safety_exclusions(tmp_path: Path) -> None:
    report = build_dynamic_bad_quote_suppression_research(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-02",
        user_id="live_bot",
        log_text=_sample_log(),
        config=_config(),
    )

    rows = {row["symbol"]: row for row in report["symbols"]}
    assert rows["SPY"]["recommended_suppression"] == "do_not_suppress"
    assert rows["SPY"]["recommendation_reason"] == "core_or_etf_symbol"
    assert rows["NEWS"]["recommended_suppression"] == "do_not_suppress"
    assert rows["NEWS"]["recommendation_reason"] == "strong_catalyst_present"
    assert rows["LATER"]["ever_accepted_later"] is True
    assert rows["LATER"]["became_tradable_later"] is True
    assert "never suppress core/ETF list symbols" in report["recommended_rules"]
    assert "never suppress if strong catalyst is present" in report["recommended_rules"]


def test_bad_quote_suppression_research_reads_history_and_writes_cli(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260702T134000000000Z_live_bot.json")
    log_dir = tmp_path / "data" / "review" / "2026-07-02"
    log_dir.mkdir(parents=True)
    (log_dir / "live.log").write_text(_sample_log(), encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text("universe:\n  symbols: [SPY, QQQ]\n", encoding="utf-8")

    json_path, text_path, report = write_dynamic_bad_quote_suppression_research(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-02",
        user_id="live_bot",
        config=_config(),
    )

    assert json_path == tmp_path / "data" / "research_metrics" / "2026-07-02" / "dynamic_bad_quote_suppression_research.json"
    assert text_path == tmp_path / "data" / "research_metrics" / "2026-07-02" / "dynamic_bad_quote_suppression_research.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["events"] >= 1
    assert {row["symbol"] for row in payload["symbols"]} >= {"BMGL", "HIST"}
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic Bad Quote Suppression Research 2026-07-02 user=live_bot" in text
    assert "| BMGL |" in render_dynamic_bad_quote_suppression_research(report)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_bad_quote_suppression_research.py"),
            "--date",
            "2026-07-02",
            "--user",
            "live_bot",
            "--project-root",
            str(tmp_path),
            "--data-dir",
            str(tmp_path / "data"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Dynamic Bad Quote Suppression Research 2026-07-02 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
