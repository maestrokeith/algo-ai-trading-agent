#!/usr/bin/env python3
"""Run a consolidated read-only end-of-day review."""
from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

import yaml

from src.dynamic_weak_catalyst_report import (
    build_dynamic_weak_catalyst_report,
    format_dynamic_weak_catalyst_report,
)
from src.review_logs import paper_full_log_path, paper_review_dir


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ReviewCommand:
    name: str
    argv: tuple[str, ...]


class CommandRunner:
    def run(self, argv: Sequence[str], *, cwd: Path = PROJECT_ROOT) -> CommandResult:
        proc = subprocess.run(
            list(argv),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def _env_name(args: argparse.Namespace) -> str:
    if bool(args.live) == bool(args.paper):
        raise ValueError("choose exactly one of --live or --paper")
    return "live" if args.live else "paper"


def _user_for_env(env: str) -> str:
    return "live_bot" if env == "live" else "paper_bot"


def _date_arg(raw: str | None) -> str:
    if raw:
        return raw
    return datetime.now(MARKET_TZ).date().isoformat()


def _bin_algo(root: Path) -> str:
    return str(root / "bin" / "algo")


def build_review_commands(*, root: Path, env: str, day: str, paper_log_path: Path | None = None) -> list[ReviewCommand]:
    env_flag = f"--{env}"
    user = _user_for_env(env)
    bin_algo = _bin_algo(root)
    paper_log = str(paper_log_path or paper_full_log_path(root, day))
    commands = [
        ReviewCommand("capture_metrics", (bin_algo, "capture-metrics", "--end-day", env_flag, "--date", day, "--user", user)),
        ReviewCommand("daily_summary", (bin_algo, "summary", day, "--user", user)),
        ReviewCommand("research_feedback", (bin_algo, "research-feedback", day, "--user", user)),
        ReviewCommand("strategy_quality", (bin_algo, "strategy-quality-report", "--date", day, "--user", user)),
        ReviewCommand("positions", (sys.executable, str(root / "scripts" / "check_positions.py"), env_flag)),
        # End-day review is intentionally read-only. Dry-run self-heal still classifies health
        # and avoids issue writes, deploys, restarts, broker actions, or config changes.
        ReviewCommand("self_heal", (bin_algo, "self-heal", env_flag, "--dry-run")),
        ReviewCommand("autoops_report", (bin_algo, "autoops", "report")),
        ReviewCommand("dynamic_entry_alignment", (bin_algo, "dynamic-entry-alignment-report", "--date", day, "--user", user)),
        ReviewCommand("dynamic_entry_adaptive", (bin_algo, "dynamic-entry-adaptive-report", "--date", day, "--user", user)),
    ]
    if env == "paper":
        commands.extend(
            [
                ReviewCommand(
                    "dynamic_funnel",
                    (bin_algo, "dynamic-funnel-report", "--date", day, "--user", user, "--log-file", paper_log),
                ),
                ReviewCommand(
                    "dynamic_forward_returns",
                    (bin_algo, "dynamic-rvol-forward-returns", "--date", day, "--user", user, "--log-path", paper_log),
                ),
            ]
        )
    else:
        commands.append(ReviewCommand("dynamic_funnel", (bin_algo, "dynamic-funnel-report", "--date", day, "--user", user)))
    return commands


def _market_open_since(day: str) -> str:
    return f"{day} 09:30:00"


def _read_log_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _platform_name() -> str:
    override = os.environ.get("ALGO_AUTOOPS_PLATFORM")
    if override:
        return override.strip() or platform.system()
    return platform.system()


def _latest_existing_file(paths: Sequence[Path]) -> Path | None:
    candidates = [path for path in paths if path.is_file()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.stat().st_mtime)[-1]


def _paper_log_candidates(root: Path, day: str) -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("ALGO_END_DAY_LOG_FILE") or os.environ.get("ALGO_PAPER_LOG_FILE")
    if configured:
        candidates.append(Path(configured))
    candidates.append(paper_full_log_path(root, day))
    candidates.append(root / "data" / "replay" / f"{day}_paper_bot.json")
    return candidates


def _collect_logs_since_market_open(
    *,
    runner: CommandRunner,
    root: Path,
    env: str,
    day: str,
    log_file: str | None,
) -> str:
    if log_file:
        print(f"END_DAY_LOG_SOURCE source=file path={log_file}")
        return _read_log_file(Path(log_file))
    env_log = os.environ.get("ALGO_END_DAY_LOG_FILE")
    if env_log:
        print(f"END_DAY_LOG_SOURCE source=file path={env_log}")
        return _read_log_file(Path(env_log))
    if _platform_name() == "Darwin" and env == "paper":
        paper_review_dir(root, day).mkdir(parents=True, exist_ok=True)
        path = _latest_existing_file(_paper_log_candidates(root, day))
        if path is not None:
            print(f"END_DAY_LOG_SOURCE source=file path={path}")
            return _read_log_file(path)
        print("END_DAY_LOG_SOURCE source=none")
        return ""
    service = os.environ.get("ALGO_LIVE_SERVICE" if env == "live" else "ALGO_PAPER_SERVICE")
    if not service:
        service = "algo.service" if env == "live" else "paper.service"
    try:
        result = runner.run(
            ["journalctl", "-u", service, "--since", _market_open_since(day), "--no-pager"],
            cwd=root,
        )
    except FileNotFoundError:
        print("END_DAY_LOG_SOURCE source=none reason=journalctl_unavailable")
        return ""
    print("END_DAY_LOG_SOURCE source=journalctl")
    return result.stdout + result.stderr


def _write_paper_full_log(root: Path, day: str, logs: str) -> Path:
    path = paper_full_log_path(root, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(logs, encoding="utf-8")
    print(f"END_DAY_LOG_WRITTEN path={path} bytes={len(logs.encode('utf-8'))}")
    return path


def _matching_lines(logs: str, patterns: Sequence[str]) -> list[str]:
    return [line for line in logs.splitlines() if any(pattern in line for pattern in patterns)]


def summarize_logs(logs: str) -> dict[str, int]:
    option_lines = _matching_lines(logs, ("OPTIONS_ALLOCATOR", "OPTIONS_ORDER", "OPTIONS_KILL_SWITCH"))
    order_lines = _matching_lines(logs, ("ORDER_SUBMITTED", "ORDER_FILLED", "ORDER_REJECTED"))
    return {
        "options_allocator_count": sum("OPTIONS_ALLOCATOR" in line for line in option_lines),
        "options_orders_count": sum("OPTIONS_ORDER" in line for line in option_lines),
        "options_kill_switch_count": sum("OPTIONS_KILL_SWITCH" in line for line in option_lines),
        "order_submitted_count": sum("ORDER_SUBMITTED" in line for line in order_lines),
        "order_filled_count": sum("ORDER_FILLED" in line for line in order_lines),
        "order_rejected_count": sum("ORDER_REJECTED" in line for line in order_lines),
    }


def _daily_summary_activity_counts(output: str) -> dict[str, int]:
    activity = {
        "submitted_orders": 0,
        "buys": 0,
        "sells": 0,
        "exits": 0,
        "pnl_missing_exits": 0,
    }
    match = re.search(r"^Activity:\s*(.+)$", output, re.MULTILINE)
    if not match:
        return activity
    tail = match.group(1)
    for key, dst in (
        ("submitted_orders", "submitted_orders"),
        ("buys", "buys"),
        ("sells", "sells"),
        ("exits", "exits"),
        ("pnl_missing_exits", "pnl_missing_exits"),
    ):
        item = re.search(rf"\b{re.escape(key)}=([0-9]+)", tail)
        if item:
            activity[dst] = int(item.group(1))
    return activity


def _parse_money(value: str | None) -> float:
    if value is None:
        return 0.0
    text = str(value).replace("$", "").replace(",", "").replace(" ", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def _daily_summary_unrealized(output: str) -> float:
    match = re.search(r"^PnL:.*?\bunrealized=\$?\s*([+-]?[0-9,]+(?:\.[0-9]+)?)", output, re.MULTILINE)
    if not match:
        return 0.0
    return _parse_money(match.group(1))


def _order_reconciliation_note(log_summary: Mapping[str, int], activity: Mapping[str, int]) -> str:
    journal_filled = int(log_summary.get("order_filled_count", 0) or 0)
    exits = int(activity.get("exits", 0) or 0)
    sells = int(activity.get("sells", 0) or 0)
    if journal_filled == 0 and (exits > 0 or sells > 0):
        alternate = "daily_summary_attribution_exits" if exits > 0 else "daily_summary_attribution_sells"
        alternate_count = exits if exits > 0 else sells
        return (
            " journal_fill_source=missing"
            f" alternate_fill_source={alternate}"
            f" alternate_fill_count={alternate_count}"
            " note=journalctl_order_fill_events_absent_but_daily_summary_has_exit_activity"
        )
    return " journal_fill_source=journalctl alternate_fill_source=none alternate_fill_count=0"


def _positions_unrealized_summary(output: str) -> dict[str, Any]:
    position_count = 0
    count_match = re.search(r"^Open positions:\s*([0-9]+)\s*$", output, re.MULTILINE)
    if count_match:
        position_count = int(count_match.group(1))

    unrealized: float | None = None
    for line in output.splitlines():
        if not line.strip().startswith("TOTAL"):
            continue
        amounts = re.findall(r"\$\s*([+-]?\s*[0-9,]+(?:\.[0-9]+)?)", line)
        if amounts:
            unrealized = _parse_money(amounts[-1])
            break

    return {
        "available": unrealized is not None,
        "unrealized": unrealized if unrealized is not None else 0.0,
        "positions": position_count,
    }


def _load_options_config(root: Path) -> Mapping[str, Any]:
    try:
        from src.config_loader import deep_merge, load_config

        config = load_config(root / "config" / "default.yaml")
        users_path = root / "config" / "users.yaml"
        if users_path.exists():
            payload = yaml.safe_load(users_path.read_text(encoding="utf-8")) or {}
            if isinstance(payload, Mapping):
                for user in payload.get("users") or []:
                    if not isinstance(user, Mapping) or str(user.get("id") or "") != "live_bot":
                        continue
                    overrides = user.get("overrides") if isinstance(user.get("overrides"), Mapping) else {}
                    config = deep_merge(config, dict(overrides))
                    break
    except Exception:
        return {}
    options = config.get("options") if isinstance(config, Mapping) else {}
    return options if isinstance(options, Mapping) else {}


def options_pilot_enabled(root: Path, env: str) -> bool:
    opts = _load_options_config(root)
    if env != "live":
        return False
    nested = opts.get("live_pilot") if isinstance(opts.get("live_pilot"), Mapping) else {}
    pilot_enabled = bool(nested.get("enabled")) if "enabled" in nested else bool(opts.get("live_pilot_enabled"))
    mode = str(opts.get("mode") or "").lower()
    return bool(opts.get("enabled")) and pilot_enabled and mode in {
        "live",
        "live_long_premium",
        "long_premium_only",
    }


def parse_self_heal_status(output: str, returncode: int) -> str:
    for line in output.splitlines():
        if "SELF_HEAL status=healthy" in line or "SELF_HEAL status=recovered" in line:
            return "healthy"
        if "SELF_HEAL status=blocked" in line or "SELF_HEAL status=codex_running" in line:
            return "blocked"
        if "SELF_HEAL status=failure_detected" in line or "SELF_HEAL status=verification_failed" in line:
            return "failure"
    return "failure" if returncode != 0 else "healthy"


def parse_autoops_success_pct(output: str) -> str:
    match = re.search(r"^- success %:\s*([0-9]+(?:\.[0-9]+)?)", output, re.MULTILINE)
    if not match:
        return "n/a"
    return f"{match.group(1)}%"


def _print_command_result(command: ReviewCommand, result: CommandResult) -> None:
    print(f"\n[{command.name}] exit_code={result.returncode}")
    text = (result.stdout + result.stderr).strip()
    if text:
        print(text)


def run_end_day_review(
    args: argparse.Namespace,
    *,
    runner: CommandRunner | None = None,
) -> int:
    root = Path(args.project_root).resolve()
    env = _env_name(args)
    day = _date_arg(args.date)
    runner = runner or CommandRunner()
    logs: str | None = None
    paper_log_path: Path | None = None
    if env == "paper":
        paper_log_path = paper_full_log_path(root, day)
        paper_review_dir(root, day).mkdir(parents=True, exist_ok=True)
        logs = _collect_logs_since_market_open(
            runner=runner,
            root=root,
            env=env,
            day=day,
            log_file=args.log_file,
        )
        _write_paper_full_log(root, day, logs)
    commands = build_review_commands(root=root, env=env, day=day, paper_log_path=paper_log_path)
    results: dict[str, CommandResult] = {}

    print(f"END_DAY_REVIEW env={env} date={day}")
    for command in commands:
        result = runner.run(command.argv, cwd=root)
        results[command.name] = result
        _print_command_result(command, result)

    if logs is None:
        logs = _collect_logs_since_market_open(
            runner=runner,
            root=root,
            env=env,
            day=day,
            log_file=args.log_file,
        )
    log_summary = summarize_logs(logs)
    weak_catalyst_report = build_dynamic_weak_catalyst_report(
        data_dir=root / "data",
        user_id=_user_for_env(env),
        day=day,
        log_text=logs,
    )
    print("\nOPTIONS_PILOT_LOG_SUMMARY since_market_open")
    print(f"- allocator events: {log_summary['options_allocator_count']}")
    print(f"- order events: {log_summary['options_orders_count']}")
    print(f"- kill switches: {log_summary['options_kill_switch_count']}")
    print("\nORDER_LOG_SUMMARY since_market_open")
    print(f"- submitted: {log_summary['order_submitted_count']}")
    print(f"- filled: {log_summary['order_filled_count']}")
    print(f"- rejected: {log_summary['order_rejected_count']}")
    daily_activity_counts = _daily_summary_activity_counts(
        results.get("daily_summary", CommandResult(0)).stdout
        + results.get("daily_summary", CommandResult(0)).stderr
    )
    daily_summary_output = (
        results.get("daily_summary", CommandResult(0)).stdout
        + results.get("daily_summary", CommandResult(0)).stderr
    )
    positions_output = (
        results.get("positions", CommandResult(0)).stdout
        + results.get("positions", CommandResult(0)).stderr
    )
    trade_attribution_unrealized = _daily_summary_unrealized(daily_summary_output)
    broker_open = _positions_unrealized_summary(positions_output)
    print(
        "ORDER_COUNT_RECONCILIATION "
        f"journal_submitted={log_summary['order_submitted_count']} "
        f"journal_filled={log_summary['order_filled_count']} "
        f"journal_rejected={log_summary['order_rejected_count']} "
        f"daily_summary_submitted={daily_activity_counts['submitted_orders']} "
        f"daily_summary_sells={daily_activity_counts['sells']} "
        f"daily_summary_exits={daily_activity_counts['exits']} "
        f"daily_summary_pnl_missing_exits={daily_activity_counts['pnl_missing_exits']} "
        "sources=journalctl,daily_summary"
        f"{_order_reconciliation_note(log_summary, daily_activity_counts)}"
    )
    if broker_open["available"] or int(broker_open["positions"]) > 0:
        print(
            "Unrealized source: broker_open_positions "
            f"broker_open_unrealized=${broker_open['unrealized']:.2f} "
            f"positions={int(broker_open['positions'])}"
        )
        print(
            "END_DAY_UNREALIZED_RECONCILIATION "
            f"trade_attribution_unrealized=${trade_attribution_unrealized:.2f} "
            f"broker_open_unrealized=${broker_open['unrealized']:.2f} "
            f"positions={int(broker_open['positions'])} "
            "sources=daily_summary,positions_command"
        )
    print("\n" + format_dynamic_weak_catalyst_report(weak_catalyst_report))

    self_heal = parse_self_heal_status(
        results.get("self_heal", CommandResult(1)).stdout
        + results.get("self_heal", CommandResult(1)).stderr,
        results.get("self_heal", CommandResult(1)).returncode,
    )
    autoops_pct = parse_autoops_success_pct(
        results.get("autoops_report", CommandResult(1)).stdout
        + results.get("autoops_report", CommandResult(1)).stderr
    )
    metrics_ok = results.get("capture_metrics", CommandResult(1)).returncode == 0
    summary_ok = results.get("daily_summary", CommandResult(1)).returncode == 0
    pilot_enabled = options_pilot_enabled(root, env)
    paper_log_missing = bool(env == "paper" and paper_log_path is not None and not paper_log_path.is_file())
    recommendation = (
        "review issues"
        if paper_log_missing
        or self_heal != "healthy"
        or log_summary["options_kill_switch_count"] > 0
        or any(result.returncode != 0 for result in results.values())
        else "leave config unchanged"
    )

    print("\nEND_DAY_REVIEW_STATUS")
    print(f"- metrics captured: {'yes' if metrics_ok else 'no'}")
    print(f"- summary generated: {'yes' if summary_ok else 'no'}")
    print(f"- self-heal: {self_heal}")
    print(f"- autoops: {autoops_pct}")
    if env == "paper":
        print(f"- paper review log: {'missing' if paper_log_missing else 'present'}")
    print(
        "- options pilot: "
        f"enabled {'yes' if pilot_enabled else 'no'}, "
        f"orders count {log_summary['options_orders_count']}, "
        f"kill switches count {log_summary['options_kill_switch_count']}"
    )
    print(f"- recommendation: {recommendation}")
    return 0 if recommendation == "leave config unchanged" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    env = parser.add_mutually_exclusive_group(required=True)
    env.add_argument("--live", action="store_true", help="Run the live end-of-day review.")
    env.add_argument("--paper", action="store_true", help="Run the paper end-of-day review.")
    parser.add_argument("--date", default=None, help="Review date. Defaults to today's New York date.")
    parser.add_argument("--log-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run_end_day_review(build_parser().parse_args(argv or sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
