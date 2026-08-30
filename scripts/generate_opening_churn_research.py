#!/usr/bin/env python3
"""Generate a research-only opening churn report from local attribution data."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.opening_churn_research import (  # noqa: E402
    latest_opening_churn_date,
    render_opening_churn_report,
    write_opening_churn_report,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a read-only report comparing opening entries to later entries."
    )
    parser.add_argument("--date", required=True, help="Trading date YYYY-MM-DD or latest.")
    parser.add_argument("--user", default="live_bot", help="User id for local artifacts.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--log-path", action="append", default=[], help="Extra log file to parse.")
    parser.add_argument("--journalctl-unit", default="algo.service", help="Optional systemd unit for read-only log enrichment.")
    parser.add_argument("--no-journal", action="store_true", help="Disable journalctl enrichment.")
    return parser


def _journal_log_path(day: str, unit: str) -> Path | None:
    if not unit or shutil.which("journalctl") is None:
        return None
    path = Path("/tmp") / f"opening_churn_{day}_{unit.replace('.', '_')}.log"
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
    day = str(args.date).strip()
    if day.lower() == "latest":
        latest = latest_opening_churn_date(data_dir=args.data_dir, user_id=args.user)
        if latest is None:
            print("No local opening churn date available.", file=sys.stderr)
            return 1
        day = latest
    log_paths = [Path(p) for p in args.log_path]
    if not args.no_journal and args.data_dir.resolve() == (PROJECT_ROOT / "data").resolve():
        journal_path = _journal_log_path(day, str(args.journalctl_unit or ""))
        if journal_path is not None:
            log_paths.append(journal_path)
    try:
        json_path, txt_path, report = write_opening_churn_report(
            data_dir=args.data_dir,
            user_id=args.user,
            day=day,
            project_root=args.project_root,
            log_paths=log_paths,
        )
    except Exception as exc:
        print(f"OPENING_CHURN_RESEARCH_FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(render_opening_churn_report(report))
    print(f"JSON: {json_path}")
    print(f"Text: {txt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
