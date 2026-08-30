#!/usr/bin/env python3
"""Generate research-only allocator suppression diagnostics."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.allocator_suppression_report import (  # noqa: E402
    render_allocator_suppression_report,
    write_allocator_suppression_report,
)


def _journal_log_path(day: str, unit: str) -> Path | None:
    if not unit or shutil.which("journalctl") is None:
        return None
    path = Path("/tmp") / f"allocator_suppression_{day}_{unit.replace('.', '_')}.log"
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research-only allocator suppression outcome report.")
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD.")
    parser.add_argument("--user", default="live_bot", help="User id for output naming.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--bars-dir", type=Path, default=None, help="Optional local intraday bar directory.")
    parser.add_argument("--log-path", action="append", type=Path, default=[], help="Additional log path. Repeatable.")
    parser.add_argument("--journalctl-unit", default="algo.service")
    parser.add_argument("--no-journal", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    log_paths = list(args.log_path)
    if not args.no_journal and args.data_dir.resolve() == (PROJECT_ROOT / "data").resolve():
        journal_path = _journal_log_path(str(args.date), str(args.journalctl_unit or ""))
        if journal_path is not None:
            log_paths.append(journal_path)
    try:
        json_path, text_path, report = write_allocator_suppression_report(
            project_root=args.project_root,
            data_dir=args.data_dir,
            day=str(args.date),
            user_id=str(args.user),
            bars_dir=args.bars_dir,
            log_paths=log_paths,
        )
    except Exception as exc:
        print(f"ALLOCATOR_SUPPRESSION_REPORT_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_allocator_suppression_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
