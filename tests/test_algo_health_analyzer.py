from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from scripts.analyze_algo_health_report import classify_reason, render_markdown


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECK_SCRIPT = PROJECT_ROOT / "scripts" / "check_algo_health.sh"
ANALYZER = PROJECT_ROOT / "scripts" / "analyze_algo_health_report.py"


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _seed_health_repo(root: Path) -> Path:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "data" / "premarket").mkdir(parents=True, exist_ok=True)
    (root / "data" / "dynamic_scan_history").mkdir(parents=True, exist_ok=True)
    _write_executable(root / "bin" / "algo", "#!/usr/bin/env bash\necho ok\n")
    (root / "data" / "premarket" / "latest_event_feed.json").write_text(
        json.dumps({"events": [{"symbol": "QQQ"}]}),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "latest_rankings.json").write_text(
        json.dumps({"rankings": [{"symbol": "QQQ"}], "catalyst_ranked_symbols": 1}),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "latest_catalysts.json").write_text(
        json.dumps({"catalysts": [{"symbol": "QQQ"}]}),
        encoding="utf-8",
    )
    (root / "data" / "premarket" / "provider_diagnostics_latest.json").write_text(
        json.dumps(
            {
                "providers": {
                    "newsapi": {
                        "http_status": 200,
                        "raw_count": 4,
                        "filtered_count": 1,
                        "rate_limited": False,
                        "reason": "ok",
                    },
                    "alpaca": {
                        "http_status": 200,
                        "raw_count": 2,
                        "filtered_count": 1,
                        "rate_limited": False,
                        "reason": "ok",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    dynamic_path = root / "data" / "dynamic_scan_history" / "20260611T130000000000Z_live_bot.json"
    dynamic_path.write_text(
        json.dumps(
            {
                "accepted": [],
                "rejected": [
                    {
                        "symbol": "QH",
                        "rejection_reason": "unstable quote",
                        "spread_pct": 29.35,
                    },
                    {
                        "symbol": "PPCB",
                        "rejection_reason": "gain filter",
                        "gain_pct": 145.9,
                        "max_day_gain_pct": 80,
                    },
                    {
                        "symbol": "GELS",
                        "rejection_reason": "below_min_price",
                        "price": 1.0,
                        "min_price": 2.0,
                    },
                    {
                        "symbol": "INDP",
                        "rejection_reason": (
                            "entry_alignment: need 5m breakout OR new intraday high OR "
                            "strong green 1m OR opening-range breakout "
                            "(got breakout=False nh=False green=False orb=False)"
                        ),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return dynamic_path


def test_analyzer_script_exists() -> None:
    assert ANALYZER.exists()


def test_parser_groups_required_rejection_reasons() -> None:
    assert classify_reason("unstable quote spread=29.35%") == "unstable_quote"
    assert classify_reason("gain filter gain=145.9 max=80") == "gain_above_max"
    assert classify_reason("below_min_price price=1.00 min=2.00") == "below_min_price"
    assert (
        classify_reason("entry_alignment: need 5m breakout OR new intraday high")
        == "entry_alignment"
    )


def test_analyzer_includes_representative_symbols_and_interpretation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_health_repo(root)
    journal = tmp_path / "journal.log"
    journal.write_text("", encoding="utf-8")

    markdown = render_markdown(
        env_name="LIVE",
        user_id="live_bot",
        root=root,
        journal_file=journal,
        severity="research",
        kind="unusual scanner rejection rate",
        detail="rejection_rate=91%",
    )

    assert "## Root-Cause Analysis" in markdown
    assert "unstable quote / spread too wide: 1" in markdown
    assert "gain above max cap: 1" in markdown
    assert "below min price: 1" in markdown
    assert "entry alignment failure: 1" in markdown
    assert "- QH spread=29.35%" in markdown
    assert "- PPCB gain=145.90%" in markdown
    assert "- GELS price=1.00 min=2.00" in markdown
    assert "Filter Quality Interpretation" in markdown
    assert "Suggested Next Action" in markdown
    assert "Data Quality Signals" in markdown
    assert "Trading Activity Context" in markdown


def test_health_dry_run_report_is_enriched(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _seed_health_repo(root)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "journalctl", "#!/usr/bin/env bash\necho 'DYNAMIC_SCAN reject QH: unstable quote spread=29.35%'\n")
    report_path = tmp_path / "algo_health_report.md"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_HEALTH_REPORT_PATH": str(report_path),
        "ALGO_HEALTH_ACCEPTED_ZERO_THRESHOLD": "0",
    }

    proc = subprocess.run(
        [str(CHECK_SCRIPT), "--dry-run", "LIVE"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    report = report_path.read_text(encoding="utf-8")
    assert "## Root-Cause Analysis" in report
    assert "Top rejection reasons:" in report
    assert "Representative Symbols" in report
    assert "Suggested Next Action" in report
