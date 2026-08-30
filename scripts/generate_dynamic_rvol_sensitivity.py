#!/usr/bin/env python3
"""Generate research-only dynamic RVOL sensitivity diagnostics."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.dynamic_rvol_sensitivity import (  # noqa: E402
    render_dynamic_rvol_sensitivity_report,
    write_dynamic_rvol_sensitivity_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Research-only analysis of hypothetical dynamic scanner RVOL thresholds."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD, or latest.")
    parser.add_argument("--user", default="paper_bot", help="User id for input selection and output naming.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--history-dir", type=Path, default=None, help="Optional dynamic_scan_history directory.")
    parser.add_argument("--bars-dir", type=Path, default=None, help="Optional local intraday bar directory.")
    parser.add_argument("--log-path", action="append", default=[], help="Extra log file to parse.")
    parser.add_argument("--journalctl-unit", default="algo.service", help="Optional systemd unit for read-only log enrichment.")
    parser.add_argument("--no-journal", action="store_true", help="Disable journalctl enrichment.")
    return parser


def _journal_log_path(day: str, unit: str) -> Path | None:
    if day == "latest" or not unit or shutil.which("journalctl") is None:
        return None
    path = Path("/tmp") / f"dynamic_rvol_sensitivity_{day}_{unit.replace('.', '_')}.log"
    cmd = [
        "journalctl",
        "-u",
        unit,
        "--since",
        f"{day} 09:30",
        "--until",
        f"{day} 16:00",
        "--no-pager",
    ]
    try:
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    path.write_text(proc.stdout, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    log_paths = [Path(path) for path in args.log_path]
    if (
        not args.no_journal
        and str(args.date).strip().lower() != "latest"
        and args.data_dir.resolve() == (PROJECT_ROOT / "data").resolve()
    ):
        journal_path = _journal_log_path(str(args.date).strip(), str(args.journalctl_unit or ""))
        if journal_path is not None:
            log_paths.append(journal_path)
    try:
        json_path, text_path, report = write_dynamic_rvol_sensitivity_report(
            data_dir=args.data_dir,
            day=args.date,
            user_id=args.user,
            history_dir=args.history_dir,
            bars_dir=args.bars_dir,
            log_paths=log_paths,
        )
    except Exception as exc:
        print(f"DYNAMIC_RVOL_SENSITIVITY_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_dynamic_rvol_sensitivity_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
