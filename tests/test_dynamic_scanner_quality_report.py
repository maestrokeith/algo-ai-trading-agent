from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.dynamic_scanner_quality_report import (
    build_dynamic_scanner_quality_report,
    render_dynamic_scanner_quality_report,
    write_dynamic_scanner_quality_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config() -> dict:
    return {"universe": {"symbols": ["AAPL", "SPY"]}, "execution": {"large_cap_symbols": ["TSLA"]}}


def _row(symbol: str, reason: str | None, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "symbol": symbol,
        "timestamp": "2026-07-01T14:00:00+00:00",
        "accepted": reason is None,
        "rejection_reason": reason,
        "price": 8.0,
        "gain_pct": 6.5,
        "volume": 50_000,
        "relative_volume": 0.8,
        "spread_pct": 8.0,
        "quality": {"atr_expansion_ratio": 0.5},
        "catalyst_score": 0,
        "news_score": 0,
        "event_score": 0,
        "article_count": 0,
    }
    base.update(overrides)
    return base


def _write_history(path: Path) -> None:
    payload = {
        "generated_at": "2026-07-01T14:00:00+00:00",
        "user_id": "live_bot",
        "candidates": [
            _row("JUNK", "spread too wide", volume=2_000, spread_pct=18.0),
            _row("JUNK", "unstable quote", volume=1_500, spread_pct=22.0),
            _row("JUNK", "unstable quote", volume=1_200, spread_pct=25.0),
            _row("PENNY", "below_min_price", price=0.8, volume=60_000, spread_pct=2.0),
            _row("ATRX", "atr_expansion", volume=80_000, spread_pct=3.0, quality={"atr_expansion_ratio": 0.1}),
            _row("ATRX", "atr_expansion", volume=90_000, spread_pct=3.2, quality={"atr_expansion_ratio": 0.2}),
            _row("ALIGN", "entry_alignment: need 5m breakout", volume=120_000, spread_pct=4.0),
            _row("ALIGN", "entry_alignment: need 5m breakout", volume=130_000, spread_pct=3.8),
            _row("SAFE", "entry_alignment: need 5m breakout", price=12.0, volume=200_000, relative_volume=1.3, spread_pct=4.0),
            _row("SAFE", None, price=12.5, volume=220_000, relative_volume=1.4, spread_pct=3.0),
            _row("PASSED", "entry_alignment: need 5m breakout", price=9.0, volume=150_000, relative_volume=1.2, spread_pct=3.5),
            _row("AAPL", "spread too wide", price=210.0, volume=2_000_000, relative_volume=1.0, spread_pct=4.0),
            _row("RIGHT.RT", "spread too wide", price=5.0, volume=10_000, spread_pct=10.0),
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _sample_log() -> str:
    return "2026-07-01T10:00:00 INFO ENTRY_EVAL_PASS symbol=PASSED route=dynamic_momentum_override reason=ok allocator_on=true"


def test_scanner_quality_repeated_symbols_and_junk_groups(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_scanner_quality_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
        config=_config(),
    )

    rows = {row["symbol"]: row for row in report["repeated_scanner_only_symbols"]}
    assert rows["JUNK"]["count_seen"] == 3
    assert rows["JUNK"]["count_rejected"] == 3
    assert rows["JUNK"]["rejection_reasons"] == {"unstable_quote": 2, "spread_too_wide": 1}
    assert rows["SAFE"]["ever_accepted"] is True
    assert "PASSED" not in rows
    safe = {row["symbol"]: row for row in report["safe_candidate_table"]}
    assert safe["PASSED"]["entry_eval_passed"] is True
    assert "AAPL" not in rows
    assert "RIGHT.RT" not in rows

    groups = report["junk_pattern_groups"]
    assert [row["symbol"] for row in groups["repeated_unstable_quote"]] == ["JUNK"]
    assert [row["symbol"] for row in groups["repeated_atr_expansion"]] == ["ATRX"]
    assert [row["symbol"] for row in groups["repeated_entry_alignment_failure"]] == ["ALIGN"]
    assert "PENNY" in {row["symbol"] for row in groups["no_catalyst_below_min_price"]}
    assert "JUNK" in {row["symbol"] for row in groups["no_catalyst_low_volume"]}


def test_scanner_quality_safe_candidates_and_recommendations(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")

    report = build_dynamic_scanner_quality_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
        config=_config(),
    )

    safe = {row["symbol"]: row for row in report["safe_candidate_table"]}
    assert set(safe) == {"SAFE", "PASSED"}
    assert safe["SAFE"]["ever_accepted"] is True
    assert safe["PASSED"]["entry_eval_passed"] is True

    recs = report["candidate_suppression_recommendations"]
    assert "suppress scanner-only symbol for rest of day after N repeated wide-spread rejects" in recs
    assert "require minimum liquidity for no-catalyst scanner-only names" in recs
    assert "require catalyst for spread exception" in recs


def test_scanner_quality_writes_artifacts_and_cli(tmp_path: Path) -> None:
    history_dir = tmp_path / "data" / "dynamic_scan_history"
    history_dir.mkdir(parents=True)
    _write_history(history_dir / "20260701T140000000000Z_live_bot.json")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "default.yaml").write_text(
        "universe:\n  symbols: [AAPL, SPY]\nexecution:\n  large_cap_symbols: [TSLA]\n",
        encoding="utf-8",
    )
    log_dir = tmp_path / "data" / "review" / "2026-07-01"
    log_dir.mkdir(parents=True)
    (log_dir / "live.log").write_text(_sample_log(), encoding="utf-8")

    json_path, text_path, report = write_dynamic_scanner_quality_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-01",
        user_id="live_bot",
        log_text=_sample_log(),
        config=_config(),
    )

    assert json_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_scanner_quality.json"
    assert text_path == tmp_path / "data" / "research_metrics" / "2026-07-01" / "dynamic_scanner_quality.md"
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["safe_candidate_review_count"] == 2
    text = text_path.read_text(encoding="utf-8")
    assert "Dynamic Scanner Quality Report 2026-07-01 user=live_bot" in text
    assert "| JUNK | 3 | 3 | unstable_quote:2, spread_too_wide:1 |" in text
    assert "safe candidate review count: 2" in render_dynamic_scanner_quality_report(report)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_dynamic_scanner_quality_report.py"),
            "--date",
            "2026-07-01",
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

    assert "Dynamic Scanner Quality Report 2026-07-01 user=live_bot" in result.stdout
    assert "JSON:" in result.stdout
    assert "Markdown:" in result.stdout
