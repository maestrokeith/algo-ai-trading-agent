#!/usr/bin/env python3
"""Capture begin/end-of-day research metrics from logs and read-only helpers."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(os.environ.get("ALGO_REPO_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_self_heal import detect_failure, redact_secrets  # noqa: E402

ET = ZoneInfo("America/New_York")
SYMBOL_RE = re.compile(r"\bsymbol=([A-Z0-9._-]+)\b")
ENTRY_RE = re.compile(r"\b(?P<symbol>[A-Z][A-Z0-9._-]{0,12})\s+ENTRY_EVAL\b(?P<body>.*)$")
KEY_VALUE_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=([^ ]+)")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Tiny subprocess seam for tests."""

    def run(self, args: Sequence[str], *, timeout: int = 20) -> CommandResult:
        try:
            proc = subprocess.run(
                list(args),
                cwd=PROJECT_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(124, "", str(exc))
        return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def today_et(now: datetime | None = None) -> str:
    return (now or datetime.now(ET)).astimezone(ET).date().isoformat()


def normalize_phase(begin_day: bool, end_day: bool) -> str:
    if begin_day == end_day:
        raise ValueError("choose exactly one of --begin-day or --end-day")
    return "begin_day" if begin_day else "end_day"


def normalize_env(live: bool, paper: bool) -> str:
    if live == paper:
        raise ValueError("choose exactly one of --live or --paper")
    return "live" if live else "paper"


def default_user(env: str) -> str:
    return "live_bot" if env == "live" else "paper_bot"


def default_service(env: str) -> str:
    if env == "live":
        return os.environ.get("ALGO_LIVE_SERVICE", "algo.service")
    return os.environ.get("ALGO_PAPER_SERVICE", "paper.service")


def report_paths(*, data_dir: Path, report_date: str, phase: str, env: str) -> tuple[Path, Path]:
    out_dir = data_dir / "research_metrics" / report_date
    stem = f"{phase}_{env}"
    return out_dir / f"{stem}.json", out_dir / f"{stem}.md"


def parse_key_values(text: str) -> dict[str, str]:
    return {match.group(1): match.group(2).strip(",") for match in KEY_VALUE_RE.finditer(text)}


def extract_symbol(line: str) -> str | None:
    match = SYMBOL_RE.search(line)
    if match:
        return match.group(1)
    entry = ENTRY_RE.search(line)
    if entry:
        return entry.group("symbol")
    return None


def parse_selected_symbols(line: str) -> list[str]:
    match = re.search(r"selected=(\[[^\]]*\])", line)
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return []
    return [str(item).upper() for item in parsed if str(item).strip()]


def unique_sorted(items: Sequence[str]) -> list[str]:
    return sorted({item for item in items if item})


def parse_log_metrics(log_text: str, *, env: str, since: str) -> dict[str, Any]:
    lines = [redact_secrets(line) for line in log_text.splitlines()]
    entry_by_route: dict[str, Counter[str]] = defaultdict(Counter)
    entry_pass_symbols: list[str] = []
    allocator_trace_symbols: list[str] = []
    allocator_actions: list[str] = []
    allocator_reject_reasons: Counter[str] = Counter()
    orders_submitted: list[str] = []
    order_confirmations: list[str] = []
    dynamic_selected: list[str] = []
    dynamic_rejected_reasons: Counter[str] = Counter()
    no_quote_symbols: list[str] = []
    blocked_after_no_quote_symbols: list[str] = []
    spread_rejects: list[str] = []
    atr_range_rejects: list[str] = []
    price_filter_rejects: list[str] = []
    catalyst_rejects: list[str] = []
    exceptions: list[str] = []

    for line in lines:
        lower = line.lower()
        if "DYNAMIC_SCAN selected=" in line:
            dynamic_selected.extend(parse_selected_symbols(line))
        if "DYNAMIC_SCAN reject" in line or "DYNAMIC_REJECT" in line:
            kv = parse_key_values(line)
            reason = kv.get("reason") or kv.get("reject_reason") or "unknown"
            dynamic_rejected_reasons[reason] += 1

        entry = ENTRY_RE.search(line)
        if entry:
            kv = parse_key_values(entry.group("body"))
            route = kv.get("route", "unknown")
            final = str(kv.get("final", "")).upper()
            entry_by_route[route]["total"] += 1
            if final in {"T", "TRUE", "1"}:
                entry_by_route[route]["pass"] += 1
            elif final in {"F", "FALSE", "0"}:
                entry_by_route[route]["fail"] += 1

        if "ENTRY_EVAL_PASS" in line:
            symbol = extract_symbol(line)
            if symbol:
                entry_pass_symbols.append(symbol)
        if "ENTRY_TO_ALLOCATOR_TRACE" in line:
            symbol = extract_symbol(line)
            if symbol:
                allocator_trace_symbols.append(symbol)
        if "ALLOCATOR ACTIONS" in line or "ALLOCATOR_ACTION" in line:
            allocator_actions.append(line)
        if "reject_reasons" in line or "reject reason" in lower or "ALLOCATOR_REJECT" in line:
            kv = parse_key_values(line)
            reason = kv.get("reason") or kv.get("reject_reason") or kv.get("reject_reasons") or line[-120:]
            allocator_reject_reasons[reason] += 1
        if "ORDER_SUBMITTED" in line:
            orders_submitted.append(line)
        if (
            "ORDER_FILLED" in line
            or "ORDER_STATUS" in line
            or "POSITION_CONFIRM" in line
            or "fill" in lower
            or "position confirmation" in lower
        ):
            order_confirmations.append(line)
        if "no_quote" in lower:
            symbol = extract_symbol(line)
            if symbol:
                no_quote_symbols.append(symbol)
        if "blocked_after_no_quote" in lower:
            symbol = extract_symbol(line)
            if symbol:
                blocked_after_no_quote_symbols.append(symbol)
        if "spread" in lower and ("reject" in lower or "skip" in lower or "wide" in lower):
            symbol = extract_symbol(line)
            if symbol:
                spread_rejects.append(symbol)
        if ("atr" in lower or "range" in lower) and ("reject" in lower or "skip" in lower):
            symbol = extract_symbol(line)
            if symbol:
                atr_range_rejects.append(symbol)
        if "price" in lower and ("reject" in lower or "skip" in lower or "filter" in lower):
            symbol = extract_symbol(line)
            if symbol:
                price_filter_rejects.append(symbol)
        if "no_catalyst" in lower or ("catalyst" in lower and ("reject" in lower or "required" in lower)):
            symbol = extract_symbol(line)
            if symbol:
                catalyst_rejects.append(symbol)
        if re.search(r"Traceback|ERROR|CRITICAL|FATAL|Exception", line):
            exceptions.append(line)

    flow_failure = detect_failure("\n".join(lines), env.upper(), since)
    missing_flow = []
    if flow_failure is not None:
        missing_flow.append(
            {
                "short_failure": flow_failure.short_failure,
                "expected_flow": flow_failure.expected_flow,
                "actual_missing_step": flow_failure.actual_missing_step,
                "fingerprint": flow_failure.fingerprint,
            }
        )

    return {
        "line_count": len(lines),
        "dynamic": {
            "selected_symbols": unique_sorted(dynamic_selected),
            "selected_count": len(unique_sorted(dynamic_selected)),
            "rejected_reasons": dict(dynamic_rejected_reasons),
        },
        "entry_lane": {
            "by_route": {route: dict(counts) for route, counts in sorted(entry_by_route.items())},
            "pass_symbols": unique_sorted(entry_pass_symbols),
            "allocator_trace_symbols": unique_sorted(allocator_trace_symbols),
        },
        "allocator": {
            "actions_count": len(allocator_actions),
            "actions": allocator_actions[-80:],
            "reject_reasons": dict(allocator_reject_reasons),
        },
        "orders": {
            "submitted_count": len(orders_submitted),
            "submitted": orders_submitted[-80:],
            "confirmation_count": len(order_confirmations),
            "confirmations": order_confirmations[-80:],
        },
        "risk_filters": {
            "no_quote_symbols": unique_sorted(no_quote_symbols),
            "blocked_after_no_quote_symbols": unique_sorted(blocked_after_no_quote_symbols),
            "spread_reject_symbols": unique_sorted(spread_rejects),
            "atr_range_reject_symbols": unique_sorted(atr_range_rejects),
            "price_filter_reject_symbols": unique_sorted(price_filter_rejects),
            "catalyst_reject_symbols": unique_sorted(catalyst_rejects),
        },
        "exceptions": exceptions[-120:],
        "missing_flow_diagnostics": missing_flow,
    }


def command_output(runner: CommandRunner, args: Sequence[str], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {"command": list(args), "exit_code": None, "stdout": "", "stderr": "", "skipped": "dry_run"}
    result = runner.run(args)
    return {
        "command": list(args),
        "exit_code": result.returncode,
        "stdout": redact_secrets(result.stdout)[-12000:],
        "stderr": redact_secrets(result.stderr)[-4000:],
    }


def _money_value(text: str) -> float | None:
    cleaned = text.replace("$", "").replace(",", "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def extract_portfolio_metrics(context: Mapping[str, Any]) -> dict[str, Any]:
    """Best-effort structured metrics from read-only helper text."""
    account = context.get("account_summary", {}) if isinstance(context.get("account_summary"), Mapping) else {}
    positions = context.get("positions", {}) if isinstance(context.get("positions"), Mapping) else {}
    text = "\n".join(str(item or "") for item in (account.get("stdout"), positions.get("stdout")))
    metrics: dict[str, Any] = {
        "equity": None,
        "cash": None,
        "buying_power": None,
        "gross_exposure": None,
        "daily_realized_pnl": None,
        "daily_unrealized_pnl": None,
    }
    patterns = {
        "equity": r"(?i)\bequity\b[^0-9$+-]*([$+-]?[0-9,]+(?:\.\d+)?)",
        "cash": r"(?i)\bcash\b[^0-9$+-]*([$+-]?[0-9,]+(?:\.\d+)?)",
        "buying_power": r"(?i)\bbuying[_ ]?power\b[^0-9$+-]*([$+-]?[0-9,]+(?:\.\d+)?)",
        "daily_realized_pnl": r"(?i)\b(?:daily_?)?realized(?:_?pnl| p/?l)?\b[^0-9$+-]*([$+-]?[0-9,]+(?:\.\d+)?)",
        "daily_unrealized_pnl": r"(?i)\b(?:daily_?)?unrealized(?:_?pnl| p/?l)?\b[^0-9$+-]*([$+-]?[0-9,]+(?:\.\d+)?)",
        "gross_exposure": r"(?i)\bgross[_ ]?exposure\b[^0-9$+-]*([$+-]?[0-9,]+(?:\.\d+)?)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match:
            metrics[key] = _money_value(match.group(1))
    if metrics["gross_exposure"] is None:
        total_line = next((line for line in text.splitlines() if line.strip().startswith("TOTAL")), "")
        money_values = re.findall(r"[$+-]?[0-9,]+(?:\.\d+)?", total_line)
        if money_values:
            metrics["gross_exposure"] = _money_value(money_values[0])
        elif metrics["equity"] is not None and metrics["cash"] is not None:
            metrics["gross_exposure"] = max(0.0, float(metrics["equity"]) - float(metrics["cash"]))
    return metrics


def collect_logs(runner: CommandRunner, *, env: str, service: str, since: str, log_file: Path | None) -> str:
    if log_file is not None:
        return log_file.read_text(encoding="utf-8", errors="replace")
    result = runner.run(["journalctl", "-u", service, "--since", since, "--no-pager"], timeout=20)
    return redact_secrets(result.stdout + result.stderr)


def collect_context(
    runner: CommandRunner,
    *,
    env: str,
    user: str,
    service: str,
    report_date: str,
    dry_run: bool,
) -> dict[str, Any]:
    mode = env
    git_branch = runner.run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    git_commit = runner.run(["git", "rev-parse", "--short", "HEAD"])
    service_state = runner.run(["systemctl", "is-active", service])
    return {
        "git": {
            "branch": git_branch.stdout.strip() or "unknown",
            "commit": git_commit.stdout.strip() or "unknown",
        },
        "service": {
            "name": service,
            "active_state": (service_state.stdout + service_state.stderr).strip() or "unknown",
        },
        "account_summary": command_output(
            runner,
            [str(PROJECT_ROOT / "bin" / "algo"), "summary", report_date, "--user", user, "--no-journal"],
            dry_run=dry_run,
        ),
        "positions": command_output(
            runner,
            [str(PROJECT_ROOT / "bin" / "algo"), "positions", f"--{mode}"],
            dry_run=dry_run,
        ),
        "open_orders": command_output(
            runner,
            [sys.executable, str(PROJECT_ROOT / "scripts" / "show_open_orders.py"), "--mode", mode, "--user", user],
            dry_run=dry_run,
        ),
    }


def build_report(
    *,
    phase: str,
    env: str,
    report_date: str,
    user: str,
    service: str,
    since: str,
    logs: str,
    context: Mapping[str, Any],
    dry_run: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    parsed_logs = parse_log_metrics(logs, env=env, since=since)
    portfolio_metrics = extract_portfolio_metrics(context)
    risk = parsed_logs["risk_filters"]
    summary = {
        "dynamic_selected_count": parsed_logs["dynamic"]["selected_count"],
        "entry_eval_total": sum(route.get("total", 0) for route in parsed_logs["entry_lane"]["by_route"].values()),
        "entry_eval_pass_count": len(parsed_logs["entry_lane"]["pass_symbols"]),
        "allocator_actions_count": parsed_logs["allocator"]["actions_count"],
        "orders_submitted_count": parsed_logs["orders"]["submitted_count"],
        "exceptions_count": len(parsed_logs["exceptions"]),
        "missing_flow_count": len(parsed_logs["missing_flow_diagnostics"]),
        "risk_reject_symbol_count": sum(len(value) for value in risk.values() if isinstance(value, list)),
    }
    return {
        "schema_version": 1,
        "generated_at": (now or datetime.now(ET)).astimezone(ET).isoformat(),
        "date": report_date,
        "phase": phase,
        "environment": env.upper(),
        "environment_label": env,
        "user": user,
        "service": service,
        "dry_run": dry_run,
        "time_window": since,
        "summary": summary,
        "context": context,
        "portfolio_metrics": portfolio_metrics,
        "logs": parsed_logs,
    }


def _table(rows: Sequence[tuple[str, Any]]) -> str:
    lines = ["| Metric | Value |", "| --- | --- |"]
    for key, value in rows:
        lines.append(f"| {key} | {value} |")
    return "\n".join(lines)


def _list_or_none(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "none"


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    logs = report["logs"]
    context = report["context"]
    portfolio = report.get("portfolio_metrics", {})
    dynamic = logs["dynamic"]
    entry = logs["entry_lane"]
    allocator = logs["allocator"]
    orders = logs["orders"]
    risk = logs["risk_filters"]
    exceptions = logs["exceptions"]
    missing = logs["missing_flow_diagnostics"]
    account = context.get("account_summary", {})
    positions = context.get("positions", {})
    open_orders = context.get("open_orders", {})
    lines = [
        f"# Research Metrics {report['environment']} {report['phase']} {report['date']}",
        "",
        "## Executive Summary",
        "",
        _table(
            [
                ("generated_at", report["generated_at"]),
                ("git", f"{context['git']['branch']}@{context['git']['commit']}"),
                ("service", f"{context['service']['name']} {context['service']['active_state']}"),
                ("equity", portfolio.get("equity")),
                ("cash", portfolio.get("cash")),
                ("buying power", portfolio.get("buying_power")),
                ("gross exposure", portfolio.get("gross_exposure")),
                ("dynamic selected", summary["dynamic_selected_count"]),
                ("entry eval total", summary["entry_eval_total"]),
                ("entry eval pass symbols", summary["entry_eval_pass_count"]),
                ("allocator actions", summary["allocator_actions_count"]),
                ("orders submitted", summary["orders_submitted_count"]),
                ("exceptions", summary["exceptions_count"]),
                ("missing flow diagnostics", summary["missing_flow_count"]),
            ]
        ),
        "",
        "## Key Metrics",
        "",
        f"- account summary exit_code={account.get('exit_code')} skipped={account.get('skipped', '')}",
        f"- positions exit_code={positions.get('exit_code')} skipped={positions.get('skipped', '')}",
        f"- open orders exit_code={open_orders.get('exit_code')} skipped={open_orders.get('skipped', '')}",
        "",
        "## Dynamic Scanner",
        "",
        f"- selected symbols: {_list_or_none(dynamic['selected_symbols'])}",
        f"- rejected reasons: {json.dumps(dynamic['rejected_reasons'], sort_keys=True)}",
        "",
        "## Entry Lane",
        "",
        f"- by route: {json.dumps(entry['by_route'], sort_keys=True)}",
        f"- ENTRY_EVAL_PASS symbols: {_list_or_none(entry['pass_symbols'])}",
        f"- ENTRY_TO_ALLOCATOR_TRACE symbols: {_list_or_none(entry['allocator_trace_symbols'])}",
        "",
        "## Allocator",
        "",
        f"- actions count: {allocator['actions_count']}",
        f"- reject reasons: {json.dumps(allocator['reject_reasons'], sort_keys=True)}",
        "",
        "## Order / Fill",
        "",
        f"- submitted count: {orders['submitted_count']}",
        f"- confirmation count: {orders['confirmation_count']}",
        "",
        "## Risk / Exposure",
        "",
        f"- no_quote: {_list_or_none(risk['no_quote_symbols'])}",
        f"- blocked_after_no_quote: {_list_or_none(risk['blocked_after_no_quote_symbols'])}",
        f"- spread rejects: {_list_or_none(risk['spread_reject_symbols'])}",
        f"- ATR/range rejects: {_list_or_none(risk['atr_range_reject_symbols'])}",
        f"- price filter rejects: {_list_or_none(risk['price_filter_reject_symbols'])}",
        f"- catalyst/no_catalyst rejects: {_list_or_none(risk['catalyst_reject_symbols'])}",
        "",
        "## Exceptions / Errors",
        "",
        "```text",
        "\n".join(exceptions) if exceptions else "none",
        "```",
        "",
        "## Research Notes / Anomalies",
        "",
    ]
    if missing:
        for item in missing:
            lines.append(f"- {item['short_failure']}: {item['actual_missing_step']}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Account / Position / Order Snapshots",
            "",
            "### Account Summary",
            "",
            "```text",
            account.get("stdout") or account.get("stderr") or "not captured",
            "```",
            "",
            "### Positions",
            "",
            "```text",
            positions.get("stdout") or positions.get("stderr") or "not captured",
            "```",
            "",
            "### Open Orders",
            "",
            "```text",
            open_orders.get("stdout") or open_orders.get("stderr") or "not captured",
            "```",
            "",
        ]
    )
    return "\n".join(str(line) for line in lines)


def write_report(report: Mapping[str, Any], *, data_dir: Path) -> tuple[Path, Path]:
    json_path, md_path = report_paths(
        data_dir=data_dir,
        report_date=str(report["date"]),
        phase=str(report["phase"]),
        env=str(report["environment_label"]),
    )
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture begin/end-of-day research metrics.")
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--begin-day", action="store_true")
    phase.add_argument("--end-day", action="store_true")
    env = parser.add_mutually_exclusive_group(required=True)
    env.add_argument("--live", action="store_true")
    env.add_argument("--paper", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="build report in memory and print target paths without writing files")
    parser.add_argument("--date", default=None, help="Report date YYYY-MM-DD. Defaults to today in ET.")
    parser.add_argument("--user", default=None, help="User id. Defaults to live_bot or paper_bot.")
    parser.add_argument("--service", default=None, help="systemd service name. Defaults by environment.")
    parser.add_argument("--since", default=None, help="journalctl window. Defaults by phase.")
    parser.add_argument("--data-dir", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--log-file", type=Path, default=None, help="Read logs from a file instead of journalctl.")
    return parser


def default_since(phase: str, report_date: str) -> str:
    if phase == "begin_day":
        return f"{report_date} 04:00:00"
    return f"{report_date} 09:25:00"


def main(argv: Sequence[str] | None = None, runner: CommandRunner | None = None) -> int:
    args = build_parser().parse_args(argv)
    phase = normalize_phase(args.begin_day, args.end_day)
    env = normalize_env(args.live, args.paper)
    report_date = args.date or today_et()
    date.fromisoformat(report_date)
    user = args.user or default_user(env)
    service = args.service or default_service(env)
    since = args.since or default_since(phase, report_date)
    active_runner = runner or CommandRunner()
    logs = collect_logs(active_runner, env=env, service=service, since=since, log_file=args.log_file)
    context = collect_context(
        active_runner,
        env=env,
        user=user,
        service=service,
        report_date=report_date,
        dry_run=args.dry_run,
    )
    report = build_report(
        phase=phase,
        env=env,
        report_date=report_date,
        user=user,
        service=service,
        since=since,
        logs=logs,
        context=context,
        dry_run=args.dry_run,
    )
    json_path, md_path = report_paths(data_dir=args.data_dir, report_date=report_date, phase=phase, env=env)
    if args.dry_run:
        print(f"DRY_RUN would write {json_path}")
        print(f"DRY_RUN would write {md_path}")
        print(render_markdown(report))
        return 0
    written_json, written_md = write_report(report, data_dir=args.data_dir)
    print(f"Wrote {written_json}")
    print(f"Wrote {written_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
