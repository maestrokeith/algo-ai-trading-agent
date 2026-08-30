"""Show rolling catalyst outcome statistics from the JSON analytics store."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.catalyst_outcomes import load_catalyst_outcome_records, summarize_catalyst_outcomes


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_float(value: Any) -> str:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "inf" if out == float("inf") else f"{out:.2f}"


def render_stats_table(summary: Mapping[str, Mapping[str, float]]) -> str:
    """Render catalyst stats as a plain text table."""
    headers = ("Catalyst", "Samples", "Win Rate", "Avg Return", "Median Return", "Profit Factor")
    rows = [headers]
    for catalyst_type in sorted(summary):
        row = summary[catalyst_type]
        rows.append(
            (
                catalyst_type,
                str(int(row.get("sample_count", row.get("count", 0.0)))),
                _fmt_pct(row.get("win_rate_pct")),
                _fmt_pct(row.get("avg_return_pct")),
                _fmt_pct(row.get("median_return_pct")),
                _fmt_float(row.get("profit_factor")),
            )
        )
    widths = [max(len(str(row[idx])) for row in rows) for idx in range(len(headers))]
    lines = []
    for idx, row in enumerate(rows):
        lines.append("  ".join(str(cell).ljust(widths[col]) for col, cell in enumerate(row)))
        if idx == 0:
            lines.append("  ".join("-" * width for width in widths))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Show rolling catalyst outcome statistics")
    parser.add_argument(
        "--path",
        default="data/analytics/catalyst_outcomes.json",
        help="Path to catalyst outcome JSON store",
    )
    args = parser.parse_args(argv)
    records = load_catalyst_outcome_records(Path(args.path))
    summary = summarize_catalyst_outcomes(records)
    if not summary:
        print("No catalyst outcomes recorded.")
        return 0
    print(render_stats_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
