"""Tests for :mod:`scripts.generate_premarket_health_report`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import scripts.generate_premarket_health_report as cli
from src.premarket_health_report import PremarketHealthReport, PremarketReportSection


def test_generate_premarket_health_report_cli_writes_output(
    monkeypatch,
    tmp_path: Path,
    capsys,
) -> None:
    output = tmp_path / "premarket.html"

    class FakeBroker:
        def __init__(self, config, paper=True):
            self.config = config
            self.paper = paper

    report = PremarketHealthReport(
        generated_at=datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc),
        ok=True,
        sections=[
            PremarketReportSection("account", True, "ready", {"equity": 1.0, "cash": 1.0, "buying_power": 1.0, "status": "ACTIVE"}),
            PremarketReportSection("news", True, "ready", {"total_items": 1}),
            PremarketReportSection("dynamic_scan", True, "ready", {"rankings": 1}),
            PremarketReportSection("open_orders", True, "none", {"count": 0, "symbols": []}),
            PremarketReportSection("exposure", True, "ready", {"positions": 0, "gross": 0.0, "net": 0.0, "inverse_etf": 0.0}),
        ],
    )

    monkeypatch.setattr(cli, "load_app_config", lambda path: {"broker": {}})
    monkeypatch.setattr(cli, "AlpacaBroker", FakeBroker)
    monkeypatch.setattr(cli, "build_premarket_health_report", lambda **kwargs: report)
    delivered: list[SimpleNamespace] = []
    monkeypatch.setattr(
        cli,
        "deliver_premarket_health_report",
        lambda report, html_path, user_label: delivered.append(SimpleNamespace(path=html_path, user=user_label)),
    )

    rc = cli.main(["--output", str(output), "--user-label", "u1", "--deliver"])

    assert rc == 0
    assert output.exists()
    assert delivered[0].path == output
    assert delivered[0].user == "u1"
    assert "pre-market health: READY" in capsys.readouterr().out
