"""Tests for :mod:`scripts.generate_daily_report`."""

from __future__ import annotations

import sys
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_daily_report import (
    _drawdown_pct_series,
    generate_report,
    generate_report_html,
    main,
    plotly_available,
    save_html,
)


def test_generate_report_html_structure() -> None:
    html = generate_report_html(
        account={"equity": 50_000.0, "pnl_today": -125.5},
        positions=[{"symbol": "AAPL", "market_value": 10_000.0, "pnl": -50}],
        trades=[
            {"strategy": "trend_long", "pnl": 25.0},
            {"strategy": "trend_long", "pnl": -150.5},
        ],
        exposure={"gross": 20.0, "net": 18.0, "sector": {"technology": 40.0}},
    )
    assert "Daily dashboard" in html
    assert "50,000.00" in html or "50000.00" in html
    assert "AAPL" in html
    assert "trend_long" in html
    assert "Total: 2" in html and "Winners: 1" in html and "Losers: 1" in html
    assert "Dynamic PnL" in html
    assert "Trade postmortem" in html
    assert "trend_long" in html
    if plotly_available():
        assert "Equity curve" in html
        assert "Daily PnL" in html
        assert "Drawdown from peak" in html
        assert "Plotly.newPlot" in html
    else:
        assert "plotly" in html.lower() and "requirements.txt" in html


def test_dynamic_performance_dashboard_metrics() -> None:
    html = generate_report_html(
        account={"equity": 50_000.0, "pnl_today": 200.0},
        positions=[],
        trades=[
            {
                "symbol": "CRWD",
                "strategy": "dynamic_universe",
                "pnl": 120.0,
                "return_pct": 6.0,
                "news_score": 8,
            },
            {
                "symbol": "NVTS",
                "strategy": "dynamic_universe",
                "pnl": -40.0,
                "return_pct": -2.0,
                "news_score": 3,
            },
            {
                "symbol": "AAPL",
                "strategy": "trend_long",
                "pnl": 500.0,
                "return_pct": 5.0,
                "news_score": 9,
            },
        ],
        exposure={"gross": 0.0, "net": 0.0, "sector": {}},
    )

    assert "Dynamic performance dashboard" in html
    assert "Dynamic win rate" in html
    assert "50.0%" in html
    assert "Average dynamic return" in html
    assert "2.00%" in html
    assert "Best dynamic trade" in html and "CRWD ($120.00, 6.00%)" in html
    assert "Worst dynamic trade" in html and "NVTS ($-40.00, -2.00%)" in html
    assert "News score" in html
    assert "<td>7+</td><td class=\"num\">1</td>" in html
    assert "<td>1-3</td><td class=\"num\">1</td>" in html


def test_trade_postmortem_section_explains_winners_and_losers() -> None:
    html = generate_report_html(
        account={"equity": 50_000.0, "pnl_today": -50.0},
        positions=[],
        trades=[
            {
                "symbol": "CRWD",
                "strategy": "dynamic_universe",
                "pnl": 100.0,
                "return_pct": 5.0,
                "catalyst_type": "ai",
                "news_score": 8,
            },
            {
                "symbol": "NVTS",
                "strategy": "dynamic_universe",
                "pnl": -150.0,
                "return_pct": -7.5,
                "news_score": 2,
            },
        ],
        exposure={"gross": 0.0, "net": 0.0, "sector": {}},
    )

    assert "Trade postmortem" in html
    assert "CRWD was a winner" in html
    assert "NVTS was a loser" in html
    assert "Review dynamic entry thresholds" in html
    assert "Largest loser exceeded largest winner" in html


def test_charts_with_portfolio_history() -> None:
    html = generate_report_html(
        account={"equity": 102_000.0, "pnl_today": 500.0},
        positions=[],
        trades=[],
        exposure={"gross": 0.0, "net": 0.0, "sector": {}},
        portfolio_history={
            "dates": ["2026-01-01", "2026-01-02", "2026-01-03"],
            "equity": [100_000.0, 101_000.0, 102_000.0],
            "daily_pnl": [0.0, 1000.0, 1000.0],
        },
    )
    assert "Performance" in html
    if plotly_available():
        assert "Plotly.newPlot" in html


def test_drawdown_series() -> None:
    dd = _drawdown_pct_series([100.0, 110.0, 105.0, 120.0])
    assert dd[0] == 0.0
    assert dd[1] == 0.0
    assert dd[2] < 0
    assert dd[3] == pytest.approx(0.0)


def test_risk_alert_when_sector_over_35() -> None:
    html = generate_report_html(
        account={"equity": 1.0, "pnl_today": 0.0},
        positions=[],
        trades=[],
        exposure={"gross": 0.0, "net": 0.0, "sector": {"technology": 36.0}},
    )
    assert "technology exposure high" in html


def test_no_risk_alert_under_threshold() -> None:
    html = generate_report_html(
        account={"equity": 1.0, "pnl_today": 0.0},
        positions=[],
        trades=[],
        exposure={"gross": 0.0, "net": 0.0, "sector": {"technology": 30.0}},
    )
    assert "exposure high" not in html
    assert "No sector exposure above 35%" in html


def test_save_html_writes_file(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "x.html"
    p = save_html("<html><body>ok</body></html>", path=out)
    assert p == out
    assert p.read_text(encoding="utf-8") == "<html><body>ok</body></html>"


def test_generate_report_writes_and_returns_path(tmp_path: Path) -> None:
    dest = tmp_path / "daily.html"
    ret = generate_report(
        account={"equity": 100.0, "pnl_today": 1.0},
        positions=[],
        trades=[],
        exposure={"gross": 0.0, "net": 0.0, "sector": {}},
        output_path=dest,
    )
    assert ret == dest
    assert "Daily dashboard" in dest.read_text(encoding="utf-8")


def test_generate_daily_report_help_does_not_write_daily_html() -> None:
    report_path = ROOT / "reports" / "daily.html"
    existed = report_path.exists()
    before_mtime = report_path.stat().st_mtime_ns if existed else None
    before_content = report_path.read_bytes() if existed else None

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "generate_daily_report.py"),
            "--help",
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0
    assert "Generate the daily AlgoSphere HTML trading report" in proc.stdout
    assert "--date" in proc.stdout
    if existed:
        assert report_path.exists()
        assert report_path.stat().st_mtime_ns == before_mtime
        assert report_path.read_bytes() == before_content
    else:
        assert not report_path.exists()


def test_generate_daily_report_cli_date_user_writes_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "daily_live_bot.html"

    class FakeUserManager:
        def __init__(self, config, users_path=None):
            self.config = config
            self.users_path = users_path

        def get_broker(self, user_id: str):
            assert user_id == "live_bot"
            return object()

        def get_user(self, user_id: str):
            assert user_id == "live_bot"
            return SimpleNamespace(config={"portfolio": {}})

    def fake_collect_daily_trading_report_data(*, broker, config, trade_date):
        assert trade_date.isoformat() == "2026-06-08"
        return SimpleNamespace(
            account={"equity": 100.0, "pnl_today": 1.0},
            positions=[],
            trades=[],
            exposure={"gross": 0.0, "net": 0.0, "sector": {}},
            portfolio_history=None,
        )

    monkeypatch.setattr("scripts.generate_daily_report.load_config", lambda _path: {})
    monkeypatch.setattr("scripts.generate_daily_report.UserManager", FakeUserManager)
    monkeypatch.setattr(
        "scripts.generate_daily_report.collect_daily_trading_report_data",
        fake_collect_daily_trading_report_data,
    )

    rc = main(
        [
            "--date",
            "2026-06-08",
            "--user",
            "live_bot",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert output.exists()
    assert "Daily dashboard" in output.read_text(encoding="utf-8")


def test_html_escapes_symbol() -> None:
    html = generate_report_html(
        account={"equity": 1.0, "pnl_today": 0.0},
        positions=[{"symbol": "X<script>", "market_value": 1.0, "pnl": 0.0}],
        trades=[],
        exposure={"gross": 0.0, "net": 0.0, "sector": {}},
    )
    # Table cell must escape user symbol (Plotly embeds its own <script> for charts).
    assert "X&lt;script&gt;" in html
