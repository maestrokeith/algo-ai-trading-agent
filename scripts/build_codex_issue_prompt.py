#!/usr/bin/env python3
"""Build a safe Codex prompt from a GitHub issue payload.

Input is the JSON produced by:

    gh issue view <number> --json number,title,body,labels,url

The output is plain text intended for `codex exec`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SAFETY_RULES = """\
Safety rules:
- This is PR generation only.
- Do not auto-merge.
- Do not deploy.
- Do not restart services.
- Do not modify live runtime state.
- Do not use broker credentials, Alpaca credentials, /etc/algo.env, or production secrets.
- Do not place, cancel, or modify orders.
- Do not modify live trading risk limits.
- Do not increase position sizing.
- Do not change allocation percentages.
- Do not change entry or exit strategy behavior.
- Do not enable live options.
- Do not disable safety gates.
- Do not loosen spread filters.
- Do not loosen volatility filters.
- Do not push directly to main.
- Keep changes scoped to software bugs, tests, diagnostics, reporter/health-check logic, or docs.
"""


VALIDATION_REQUIREMENTS = """\
Validation requirements:
- Add or update tests for the fix.
- Run:
  bash -n scripts/report_algo_failure_to_github.sh
  bash -n scripts/check_algo_health.sh
  python -m py_compile scripts/analyze_algo_health_report.py
  PYTHONPATH=. pytest tests/test_failure_reporter.py tests/test_algo_health_check.py tests/test_algo_health_analyzer.py -v
- Prefer running the full suite:
  PYTHONPATH=. pytest tests/ -q
- If a full suite cannot be completed, document exactly why.
"""


RESEARCH_RULE = (
    "If the issue appears to be a strategy, research, performance, threshold, "
    "allocation, risk, or trading-behavior issue rather than a software bug, do "
    "not change trading logic. Instead, add a GitHub issue comment explaining "
    "why human review is required. Only diagnostics, tests, or docs may be "
    "changed for that class of issue."
)


def _label_names(issue: dict[str, Any]) -> list[str]:
    labels = issue.get("labels") or []
    names: list[str] = []
    for item in labels:
        if isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
        elif isinstance(item, str):
            names.append(item)
    return names


def _field_from_body(body: str, names: tuple[str, ...]) -> str:
    for line in body.splitlines():
        stripped = line.strip().lstrip("-").strip()
        for name in names:
            prefix = f"{name}:"
            if stripped.lower().startswith(prefix.lower()):
                return stripped[len(prefix) :].strip()
            key = f"{name}="
            if stripped.lower().startswith(key.lower()):
                return stripped[len(key) :].strip()
    return ""


def _environment_from_issue(title: str, body: str, labels: list[str]) -> str:
    lowered_labels = {label.lower() for label in labels}
    if "environment:paper" in lowered_labels:
        return "PAPER"
    if "environment:live" in lowered_labels:
        return "LIVE"
    combined = f"{title}\n{body}".upper()
    if "[PAPER]" in combined or "ENVIRONMENT: PAPER" in combined or "ENVIRONMENT=PAPER" in combined:
        return "PAPER"
    if "[LIVE]" in combined or "ENVIRONMENT: LIVE" in combined or "ENVIRONMENT=LIVE" in combined:
        return "LIVE"
    return "UNKNOWN"


def _runtime_context(issue: dict[str, Any], labels: list[str]) -> dict[str, str]:
    title = str(issue.get("title", ""))
    body = issue.get("body") or ""
    return {
        "environment": _environment_from_issue(title, body, labels),
        "hostname": _field_from_body(body, ("Hostname", "Host")) or "unknown",
        "service": _field_from_body(body, ("Service Name", "Unit", "Service")) or "unknown",
        "failure_source": _field_from_body(body, ("Failure Source",)) or "unknown",
    }


def build_prompt(issue: dict[str, Any]) -> str:
    """Return a complete Codex prompt for a GitHub issue."""

    number = issue.get("number", "")
    title = issue.get("title", "")
    body = issue.get("body") or ""
    url = issue.get("url", "")
    labels = _label_names(issue)
    label_text = ", ".join(labels) if labels else "(none)"
    context = _runtime_context(issue, labels)

    return "\n".join(
        [
            f"Fix GitHub issue #{number} in this repository.",
            "",
            f"Issue title: {title}",
            f"Issue URL: {url}",
            f"Issue labels: {label_text}",
            "",
            "Runtime context:",
            f"Environment: {context['environment']}",
            f"Hostname: {context['hostname']}",
            f"Service name: {context['service']}",
            f"Failure source: {context['failure_source']}",
            "",
            "Issue body:",
            "```markdown",
            body,
            "```",
            "",
            SAFETY_RULES.rstrip(),
            "",
            VALIDATION_REQUIREMENTS.rstrip(),
            "",
            "Required workflow behavior:",
            "- Modify code only when a safe software fix is appropriate.",
            "- Add or update regression tests when code changes are made.",
            "- Preserve all hard trading safety controls.",
            "- Open a pull request only; do not merge it.",
            "- Include a concise summary and validation results in the final response.",
            "",
            RESEARCH_RULE,
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a Codex prompt from issue JSON.")
    parser.add_argument("--issue-json", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    with args.issue_json.open(encoding="utf-8") as handle:
        issue = json.load(handle)
    args.output.write_text(build_prompt(issue), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
