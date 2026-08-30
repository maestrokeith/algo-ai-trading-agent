#!/usr/bin/env python3
"""Run read-only daily operations jobs and persist their outputs."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OpsPaths:
    """Filesystem locations for one daily operations run."""

    project_root: Path
    report_date: str
    reports_dir: Path
    logs_dir: Path


@dataclass(frozen=True)
class CommandSpec:
    """A generated operations command and its persisted output target."""

    name: str
    argv: tuple[str, ...]
    output_path: Path
    log_path: Path


def parse_report_date(value: str | None, *, now: datetime | None = None) -> str:
    """Resolve a report date from CLI input or the current America/New_York date."""
    if value is None or value.strip().lower() == "today":
        resolved = (now or datetime.now(ET)).astimezone(ET).date().isoformat()
    else:
        resolved = value.strip()
    date.fromisoformat(resolved)
    return resolved


def build_ops_paths(*, project_root: Path, report_date: str) -> OpsPaths:
    """Return standard report and log directories for an operations date."""
    return OpsPaths(
        project_root=project_root,
        report_date=report_date,
        reports_dir=project_root / "reports" / "daily" / report_date,
        logs_dir=project_root / "data" / "logs",
    )


def build_command_specs(*, job: str, user: str, paths: OpsPaths, replay_max_ticks: int | None = None) -> list[CommandSpec]:
    """Build read-only commands for a daily operations job."""
    py = os.environ.get("PYTHON", sys.executable)
    bin_algo = str(paths.project_root / "bin" / "algo")
    scripts_dir = paths.project_root / "scripts"
    specs = {
        "premarket-ready": [
            CommandSpec(
                name="premarket_ready",
                argv=(bin_algo, "premarket-ready"),
                output_path=paths.reports_dir / "premarket_readiness.txt",
                log_path=paths.logs_dir / f"ops_premarket_ready_{paths.report_date}.log",
            )
        ],
        "daily-summary": [
            CommandSpec(
                name="daily_summary",
                argv=(bin_algo, "summary", paths.report_date, "--user", user),
                output_path=paths.reports_dir / "daily_summary.txt",
                log_path=paths.logs_dir / f"ops_daily_summary_{paths.report_date}.log",
            )
        ],
        "postmarket-analytics": [
            CommandSpec(
                name="catalyst_stats",
                argv=(py, str(scripts_dir / "show_catalyst_stats.py")),
                output_path=paths.reports_dir / "catalyst_stats.txt",
                log_path=paths.logs_dir / f"ops_catalyst_stats_{paths.report_date}.log",
            ),
            CommandSpec(
                name="profitability_attribution",
                argv=(
                    py,
                    str(scripts_dir / "generate_profitability_attribution_report.py"),
                    "--date",
                    paths.report_date,
                    "--user",
                    user,
                ),
                output_path=paths.reports_dir / "profitability_attribution.txt",
                log_path=paths.logs_dir / f"ops_profitability_attribution_{paths.report_date}.log",
            ),
        ],
        "replay-summary": [
            CommandSpec(
                name="replay_summary",
                argv=tuple(
                    str(part)
                    for part in (
                        py,
                        scripts_dir / "replay_market_session.py",
                        "--date",
                        paths.report_date,
                        "--user",
                        user,
                        "--broker-mock",
                        *(("--max-ticks", replay_max_ticks) if replay_max_ticks is not None else ()),
                    )
                ),
                output_path=paths.reports_dir / "replay_summary.txt",
                log_path=paths.logs_dir / f"ops_replay_summary_{paths.report_date}.log",
            )
        ],
        "research-feedback": [
            CommandSpec(
                name="research_feedback",
                argv=(
                    py,
                    str(scripts_dir / "generate_research_feedback.py"),
                    "--date",
                    paths.report_date,
                    "--user",
                    user,
                ),
                output_path=paths.reports_dir / "research_feedback.txt",
                log_path=paths.logs_dir / f"ops_research_feedback_{paths.report_date}.log",
            )
        ],
        "weekly-research-feedback": [
            CommandSpec(
                name="weekly_research_feedback",
                argv=(
                    py,
                    str(scripts_dir / "generate_research_feedback.py"),
                    "--date",
                    paths.report_date,
                    "--user",
                    user,
                    "--weekly",
                ),
                output_path=paths.reports_dir / "weekly_research_feedback.txt",
                log_path=paths.logs_dir / f"ops_weekly_research_feedback_{paths.report_date}.log",
            )
        ],
    }
    if job not in specs:
        raise ValueError(f"Unsupported command job: {job}")
    return specs[job]


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def run_command(spec: CommandSpec, *, cwd: Path) -> int:
    """Run a command, writing stdout/stderr to report and log artifacts."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(cwd)
    proc = subprocess.run(spec.argv, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    header = f"$ {' '.join(spec.argv)}\nexit_code={proc.returncode}\n\n"
    output = header + proc.stdout
    if proc.stderr:
        output += "\n[stderr]\n" + proc.stderr
    _write_text(spec.output_path, output)
    _write_text(spec.log_path, output)
    return proc.returncode


def _startup_log_candidates(project_root: Path, explicit_paths: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    candidates.extend(explicit_paths)
    for base in (project_root / "data" / "logs", project_root / "logs"):
        if base.exists():
            candidates.extend(path for path in sorted(base.glob("*.log")) if not path.name.startswith("ops_startup_validation_"))
    return [path for path in candidates if path.exists() and path.is_file()]


def _journal_startup_lines(*, unit: str | None, report_date: str) -> list[str]:
    if not unit:
        return []
    try:
        proc = subprocess.run(
            ["journalctl", "-u", unit, "--since", f"{report_date} 09:30:00", "--no-pager"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"journalctl -u {unit}: unreadable {exc}"]
    lines = proc.stdout.splitlines()
    if proc.stderr:
        lines.extend(f"journalctl -u {unit}: {line}" for line in proc.stderr.splitlines())
    return [f"journalctl -u {unit}: {line}" for line in lines if "PREMARKET_STARTUP_ARTIFACTS" in line]


def validate_startup_logs(
    *,
    project_root: Path,
    paths: OpsPaths,
    algo_logs: list[Path],
    journal_unit: str | None = None,
) -> int:
    """Persist a startup artifact validation report from local algo logs."""
    matches: list[str] = []
    for path in _startup_log_candidates(project_root, algo_logs):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            matches.append(f"{path}: unreadable {exc}")
            continue
        for line in lines:
            if "PREMARKET_STARTUP_ARTIFACTS" in line:
                matches.append(f"{path}: {line}")
    matches.extend(_journal_startup_lines(unit=journal_unit, report_date=paths.report_date))

    if not matches:
        status = 1
        body = "PREMARKET_STARTUP_ARTIFACTS not found in local algo logs.\n"
    else:
        stale_terms = ("status=stale", "status=missing", "fresh=false")
        fresh = any(("status=fresh" in line or "fresh=true" in line) for line in matches)
        stale_or_missing = any(any(term in line for term in stale_terms) for line in matches)
        status = 0 if fresh and not stale_or_missing else 1
        body = "\n".join(matches) + "\n"
        body += f"\nstartup_validation={'ok' if status == 0 else 'failed'}\n"

    output_path = paths.reports_dir / "startup_validation.txt"
    log_path = paths.logs_dir / f"ops_startup_validation_{paths.report_date}.log"
    _write_text(output_path, body)
    _write_text(log_path, body)
    return status


def _parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run read-only daily AlgoSphere operations jobs.")
    parser.add_argument(
        "job",
        choices=(
            "premarket-ready",
            "startup-validation",
            "daily-summary",
            "postmarket-analytics",
            "replay-summary",
            "research-feedback",
            "weekly-research-feedback",
        ),
    )
    parser.add_argument("--date", default="today", help="Report date YYYY-MM-DD or today.")
    parser.add_argument("--user", default="live_bot", help="User id for report-generating jobs.")
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--algo-log", action="append", type=Path, default=[], help="Algo log file to inspect for startup validation.")
    parser.add_argument("--journal-unit", default=None, help="Optional systemd unit to inspect with journalctl for startup validation.")
    parser.add_argument("--now", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--replay-max-ticks", type=int, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        report_date = parse_report_date(args.date, now=_parse_now(args.now))
    except ValueError:
        print("Use date as YYYY-MM-DD, e.g. 2026-06-07", file=sys.stderr)
        return 2

    project_root = args.project_root.resolve()
    paths = build_ops_paths(project_root=project_root, report_date=report_date)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    if args.job == "startup-validation":
        return validate_startup_logs(
            project_root=project_root,
            paths=paths,
            algo_logs=args.algo_log,
            journal_unit=args.journal_unit,
        )

    codes = [
        run_command(spec, cwd=project_root)
        for spec in build_command_specs(job=args.job, user=args.user, paths=paths, replay_max_ticks=args.replay_max_ticks)
    ]
    return max(codes) if codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
