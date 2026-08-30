"""Tests for catalyst stats CLI rendering."""

from __future__ import annotations

from scripts.show_catalyst_stats import main, render_stats_table


def test_render_stats_table_includes_rolling_metrics() -> None:
    table = render_stats_table(
        {
            "earnings": {
                "sample_count": 2.0,
                "win_rate_pct": 50.0,
                "avg_return_pct": 1.0,
                "median_return_pct": 1.0,
                "profit_factor": 2.0,
            }
        }
    )

    assert "Catalyst" in table
    assert "earnings" in table
    assert "50.00%" in table
    assert "2.00" in table


def test_main_prints_no_data_for_empty_store(tmp_path, capsys) -> None:
    assert main(["--path", str(tmp_path / "missing.json")]) == 0

    out = capsys.readouterr().out
    assert "No catalyst outcomes recorded." in out
