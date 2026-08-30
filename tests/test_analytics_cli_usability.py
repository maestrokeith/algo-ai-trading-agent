from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src.trade_attribution import attribution_daily_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env_without_pythonpath() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def test_show_catalyst_stats_direct_execution_without_pythonpath(tmp_path: Path) -> None:
    path = tmp_path / "catalyst_outcomes.json"
    path.write_text(
        json.dumps(
            {
                "outcomes": [
                    {
                        "symbol": "AAPL",
                        "catalyst_type": "earnings",
                        "realized_return_pct": 2.5,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "show_catalyst_stats.py"), "--path", str(path)],
        cwd=tmp_path,
        env=_env_without_pythonpath(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "earnings" in proc.stdout


def test_profitability_report_direct_execution_without_pythonpath_and_latest(tmp_path: Path) -> None:
    path = attribution_daily_path(data_dir=tmp_path, user_id="live_bot", day="2026-06-07")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "date": "2026-06-07",
                "user_id": "live_bot",
                "exits": [{"symbol": "AAPL", "entry_route": "core_rebuild", "pnl": 10.0}],
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "generate_profitability_attribution_report.py"),
            "--date",
            "latest",
            "--user",
            "live_bot",
            "--data-dir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=_env_without_pythonpath(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "Profitability Attribution 2026-06-07 [live_bot]" in proc.stdout
    assert (tmp_path / "profitability_attribution" / "daily" / "2026-06-07_live_bot.json").exists()


def test_replay_market_session_direct_execution_imports_without_pythonpath(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "replay_market_session.py"), "--help"],
        cwd=tmp_path,
        env=_env_without_pythonpath(),
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "YYYY-MM-DD or latest" in proc.stdout
