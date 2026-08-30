#!/usr/bin/env python3
"""Collect recent algo logs and produce a local/OpenAI debug report."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.debug_report_cleanup import cleanup_debug_reports


IMPORTANT_MARKERS = (
    "DYNAMIC_ACCEPTED",
    "DYNAMIC_SELECTED",
    "DYNAMIC_SCAN",
    "DYNAMIC_REJECTION_SUMMARY",
    "ENTRY_EVAL",
    "ALLOCATOR",
    "CORE_REBUILD",
    "BUY",
    "SELL",
    "SKIP",
    "EXIT_",
    "ERROR",
    "Traceback",
    "APIError",
)

PROMPT_TEMPLATE = """You are reviewing AlgoSphere live trading logs.

Focus on production safety and concise operator guidance. Identify:
- critical bugs
- unsafe buys
- dynamic scanner behavior
- allocator behavior
- exit/order issues
- core_rebuild activity
- recommendation: leave running / restart / fix now / stop trading

Return a concise Markdown report with evidence from the logs. If logs are empty
or inconclusive, say that explicitly and list what evidence is missing.

Filtered logs:
```text
{logs}
```
"""


def _timestamp(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def collect_journal_logs(since: str) -> tuple[str, str | None]:
    """Return raw journal output and an optional collection warning."""

    cmd = ("journalctl", "-u", "algo", "--since", since, "--no-pager")
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except FileNotFoundError:
        return "", "journalctl_missing"
    except subprocess.TimeoutExpired:
        return "", "journalctl_timeout"
    except Exception as exc:
        return "", f"journalctl_error:{type(exc).__name__}:{exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "journalctl_failed").strip()
        return proc.stdout or "", f"journalctl_failed:{detail}"
    return proc.stdout or "", None


def filter_important_lines(raw_logs: str) -> list[str]:
    """Keep only log lines containing known trading/debug markers."""

    return [
        line
        for line in raw_logs.splitlines()
        if any(marker in line for marker in IMPORTANT_MARKERS)
    ]


def _write_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _gzip_file(source: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, gzip.open(dest, "wb") as gz:
        shutil.copyfileobj(src, gz)
    return dest


def write_log_artifacts(
    output_dir: Path,
    *,
    timestamp: str,
    filtered_lines: Sequence[str],
    warning: str | None = None,
) -> dict[str, Path]:
    header = [
        f"# algo debug log timestamp={timestamp}",
        "# source=journalctl -u algo",
    ]
    if warning:
        header.append(f"# warning={warning}")
    if filtered_lines:
        body = "\n".join(filtered_lines)
    else:
        body = "# no matching important log lines found"
    content = "\n".join(header + ["", body, ""])

    timestamp_log = _write_text(output_dir / f"algo_debug_{timestamp}.log", content)
    latest_log = _write_text(output_dir / "algo_debug_latest.log", content)
    timestamp_gz = _gzip_file(timestamp_log, output_dir / f"algo_debug_{timestamp}.log.gz")
    latest_gz = _gzip_file(latest_log, output_dir / "algo_debug_latest.log.gz")
    return {
        "timestamp_log": timestamp_log,
        "latest_log": latest_log,
        "timestamp_gz": timestamp_gz,
        "latest_gz": latest_gz,
    }


def build_prompt(filtered_lines: Sequence[str], *, warning: str | None = None) -> str:
    if filtered_lines:
        logs = "\n".join(filtered_lines)
    else:
        logs = "# no matching important log lines found"
    if warning:
        logs = f"# collection_warning={warning}\n{logs}"
    return PROMPT_TEMPLATE.format(logs=logs)


def _extract_response_text(payload: dict[str, Any]) -> str:
    text = payload.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    parts: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
    return "\n".join(parts).strip() or json.dumps(payload, indent=2, sort_keys=True)


def call_openai_responses_api(*, model: str, prompt: str, api_key: str | None = None) -> str:
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is required unless --dry-run is used")
    body = json.dumps({"model": model, "input": prompt}).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"openai_http_error:{exc.code}:{detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"openai_url_error:{exc.reason}") from exc
    return _extract_response_text(payload)


def write_analysis_artifacts(output_dir: Path, *, timestamp: str, content: str) -> dict[str, Path]:
    timestamp_md = _write_text(output_dir / f"chatgpt_analysis_{timestamp}.md", content)
    latest_md = _write_text(output_dir / "chatgpt_analysis_latest.md", content)
    return {"timestamp_md": timestamp_md, "latest_md": latest_md}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send recent algo debug logs to OpenAI for operator analysis.")
    parser.add_argument("--since", default="30 minutes ago")
    parser.add_argument("--model", default=os.getenv("OPENAI_DEBUG_MODEL") or "gpt-4.1-mini")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / "debug")
    parser.add_argument("--retention-days", type=int, default=5)
    parser.add_argument("--no-cleanup", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    ts = _timestamp()
    raw_logs, warning = collect_journal_logs(args.since)
    filtered_lines = filter_important_lines(raw_logs)
    log_paths = write_log_artifacts(
        output_dir,
        timestamp=ts,
        filtered_lines=filtered_lines,
        warning=warning,
    )
    prompt = build_prompt(filtered_lines, warning=warning)

    if args.dry_run:
        analysis = (
            f"# DRY RUN OpenAI Algo Debug Report\n\n"
            f"- model: {args.model}\n"
            f"- since: {args.since}\n"
            f"- filtered_lines: {len(filtered_lines)}\n"
            f"- openai_api_called: false\n\n"
            "## Prompt That Would Be Sent\n\n"
            f"{prompt}"
        )
    else:
        response_text = call_openai_responses_api(model=args.model, prompt=prompt)
        analysis = (
            f"# OpenAI Algo Debug Report\n\n"
            f"- model: {args.model}\n"
            f"- since: {args.since}\n"
            f"- filtered_lines: {len(filtered_lines)}\n\n"
            f"{response_text.rstrip()}\n"
        )
    analysis_paths = write_analysis_artifacts(output_dir, timestamp=ts, content=analysis)

    project_root = PROJECT_ROOT
    try:
        project_root = output_dir.parents[1] if output_dir.name == "debug" and output_dir.parent.name == "reports" else PROJECT_ROOT
    except IndexError:
        project_root = PROJECT_ROOT
    cleanup_events = cleanup_debug_reports(
        project_root,
        retention_days=args.retention_days,
        enabled=not args.no_cleanup,
    )

    print(f"DEBUG_LOG path={log_paths['timestamp_log']}")
    print(f"DEBUG_LOG_GZ path={log_paths['timestamp_gz']}")
    print(f"DEBUG_LOG_LATEST path={log_paths['latest_log']}")
    print(f"DEBUG_LOG_LATEST_GZ path={log_paths['latest_gz']}")
    print(f"CHATGPT_ANALYSIS path={analysis_paths['timestamp_md']}")
    print(f"CHATGPT_ANALYSIS_LATEST path={analysis_paths['latest_md']}")
    if args.dry_run:
        print("\n" + analysis)
    for event in cleanup_events:
        print(event.log_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
