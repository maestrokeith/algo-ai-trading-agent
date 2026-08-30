"""Tests for dynamic entry rejection explainability report."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import src.dynamic_entry_rejection_report as der
from src.dynamic_entry_rejection_report import (
    build_dynamic_entry_rejection_report,
    classify_dynamic_entry_rejection,
    render_dynamic_entry_rejection_report,
    write_dynamic_entry_rejection_report,
)


def test_dynamic_entry_rejection_report_counts_live_reasons(tmp_path: Path) -> None:
    log_text = "\n".join(
        [
            "Jul 06 11:42:00 host python[1]: ENTRY_EVAL symbol=IREG route=dynamic_momentum_override final=F trend=F vol=F reason=trend=F vol=F",
            "Jul 06 11:42:01 host python[1]: ENTRY_EVAL symbol=IRE route=dynamic_momentum_override final=F reason=relative_volume 0.22 < 0.30",
            "Jul 06 11:42:02 host python[1]: ENTRY_EVAL symbol=AVGO route=dynamic_momentum_override final=F reason=trend filter: 20 EMA slope not positive",
            "Jul 06 11:42:03 host python[1]: ORDER_SKIP symbol=OPEN reason=soft_cap: no buy headroom under cap",
            "Jul 06 11:42:04 host python[1]: ORDER_SKIP symbol=OPEN reason=portfolio replacement: weakest META bars_held=0 < max_position_age_bars=20",
            "Jul 06 11:42:05 host python[1]: ENTRY_EVAL symbol=ONDS route=dynamic_momentum_override final=F reason=below_min_day_gain gain=1.2 min=2.0",
        ]
    )
    report = build_dynamic_entry_rejection_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-06",
        user_id="live_bot",
        log_text=log_text,
    )
    assert report["counts"]["trend_filter"] == 1
    assert report["counts"]["volume_filter"] == 1
    assert report["counts"]["ema_slope"] == 1
    assert report["counts"]["portfolio_cap"] == 1
    assert report["counts"]["replacement_logic"] == 1
    assert report["counts"]["gain_threshold"] == 1
    rendered = render_dynamic_entry_rejection_report(report)
    assert "| volume_filter | 1 |" in rendered
    assert "soft_cap" in rendered


def test_write_dynamic_entry_rejection_report_outputs_json_and_markdown(tmp_path: Path) -> None:
    json_path, text_path, report = write_dynamic_entry_rejection_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-06",
        user_id="live_bot",
        log_text="ENTRY_EVAL symbol=OPEN route=dynamic_momentum_override final=F reason=no_decision",
    )
    assert json_path.exists()
    assert text_path.exists()
    assert report["counts"]["no_decision"] == 1


def test_dynamic_entry_rejection_report_reads_live_journalctl_when_files_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\n".join(
                [
                    "Jul 06 10:00:00 host python[1]: OPEN ENTRY_EVAL final=F reason=soft_cap: no buy headroom under cap",
                    "Jul 06 10:01:00 host python[1]: SKIP OPEN: portfolio replacement: weakest META bars_held=0 < max_position_age_bars=20",
                    "Jul 06 10:02:00 host python[1]: IREG ENTRY_EVAL final=F reason=no_decision",
                    "Jul 06 10:03:00 host python[1]: AVGO ENTRY_EVAL final=F reason=trend filter: 20 EMA slope not positive",
                    "Jul 06 10:04:00 host python[1]: SKIP ONDS: dynamic momentum rank: not in top 3",
                ]
            ),
            stderr="",
        )

    monkeypatch.setenv("ALGO_LIVE_SERVICE", "algosphere-live.service")
    monkeypatch.setattr(der.subprocess, "run", fake_run)

    report = build_dynamic_entry_rejection_report(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        day="2026-07-06",
        user_id="live_bot",
    )

    assert calls[0][:3] == ["journalctl", "-u", "algosphere-live.service"]
    assert report["total_rejections"] == 5
    assert report["counts"]["portfolio_cap"] == 1
    assert report["counts"]["replacement_logic"] == 1
    assert report["counts"]["ema_slope"] == 1
    assert report["counts"]["momentum_rank"] == 1
    assert report["counts"]["no_decision"] == 1
    rendered = render_dynamic_entry_rejection_report(report)
    assert "## Examples" in rendered
    assert "dynamic momentum rank" in rendered


@pytest.mark.parametrize(
    ("line", "bucket"),
    [
        ("OPEN ENTRY_EVAL final=F reason=soft_cap: no buy headroom under cap", "portfolio_cap"),
        ("SKIP OPEN: portfolio replacement: weakest META bars_held=0", "replacement_logic"),
        ("AVGO ENTRY_EVAL final=F reason=trend filter: 20 EMA slope not positive", "ema_slope"),
        ("SKIP ONDS: dynamic momentum rank: not in top 3", "momentum_rank"),
        ("IREG ENTRY_EVAL final=F reason=no_decision", "no_decision"),
        ("ABCD ENTRY_EVAL final=F vol=F reason=RVOL 0.2 < 0.5", "volume_filter"),
    ],
)
def test_dynamic_entry_rejection_reason_classification(line: str, bucket: str) -> None:
    assert classify_dynamic_entry_rejection(line) == bucket
