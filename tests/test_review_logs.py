from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.review_logs import ensure_paper_review_log, market_day, paper_full_log_path, paper_review_dir

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ensure_paper_review_log_creates_dated_directory_and_file(tmp_path: Path) -> None:
    path = ensure_paper_review_log(tmp_path, "2026-06-27")

    assert path == tmp_path / "data" / "review" / "2026-06-27" / "paper_full.log"
    assert path.is_file()
    assert paper_review_dir(tmp_path, "2026-06-27").is_dir()
    assert paper_full_log_path(tmp_path, "2026-06-27") == path


def test_market_day_uses_new_york_date() -> None:
    now = datetime(2026, 6, 28, 2, 30, tzinfo=ZoneInfo("UTC"))

    assert market_day(now) == "2026-06-27"


def test_run_alpaca_loop_prepares_paper_review_log_before_runtime() -> None:
    source = (PROJECT_ROOT / "scripts" / "run_alpaca_loop.py").read_text(encoding="utf-8")

    assert "from src.review_logs import ensure_paper_review_log" in source
    assert 'if "--paper" in sys.argv or "--live" not in sys.argv:' in source
    assert "ensure_paper_review_log(PROJECT_ROOT)" in source
    assert "PAPER_REVIEW_LOG path=" in source
