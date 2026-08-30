#!/usr/bin/env python3
"""Deterministic low-cost precheck for local Codex issue processing."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

EXPLICIT_LINK_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:closes|fixes|resolves)\s+#(?P<number>\d+)\b|"
    r"^\s*(?:[-*]\s*)?(?:issue:\s*#|autoops-issue:\s*)(?P<number2>\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)

WORK_UNIT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("order_reconciliation_mismatch", ("order_count_reconciliation", "journal_filled", "daily_summary_sells")),
    ("entry_to_allocator_drop", ("entry_to_allocator", "allocator", "no order")),
    ("missing_exit_attribution", ("missing attribution", "pnl_missing_exits", "exit attribution")),
    ("stale_market_data_noise", ("stale market", "bad_quote", "unstable_quote")),
    ("strategy_opportunity_loss", ("missed opportunity", "under-trading", "not growing", "losing money")),
    ("deployment_or_service_issue", ("deploy", "service", "systemd", "restart")),
)


@dataclass(frozen=True)
class DuplicateResult:
    duplicate: bool
    source: str
    pr: int | None = None
    url: str | None = None


@dataclass(frozen=True)
class PrecheckResult:
    classification: str
    codex_required: bool
    reason: str
    estimated_scope: str
    label: str | None = None
    close: bool = False
    duplicate: DuplicateResult = DuplicateResult(False, "none")
    work_units: tuple[str, ...] = ()


def _run_json(args: list[str], *, cwd: Path) -> Any:
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError:
        return None


def _label_names(payload: Mapping[str, Any]) -> set[str]:
    labels: set[str] = set()
    for label in payload.get("labels") or []:
        if isinstance(label, Mapping):
            name = label.get("name")
        else:
            name = label
        if name:
            labels.add(str(name))
    return labels


def _load_autoops_config(root: Path) -> dict[str, Any]:
    path = root / "config" / "default.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        payload = {}
    section = payload.get("autoops") if isinstance(payload, Mapping) else {}
    return dict(section) if isinstance(section, Mapping) else {}


def _explicitly_links_issue(text: str, issue: int) -> bool:
    for match in EXPLICIT_LINK_RE.finditer(text or ""):
        value = match.group("number") or match.group("number2")
        if value and int(value) == issue:
            return True
    return False


def duplicate_check(issue: int, branch: str, *, root: Path) -> DuplicateResult:
    head = _run_json(
        ["gh", "pr", "list", "--state", "open", "--head", branch, "--json", "number,url,headRefName"],
        cwd=root,
    )
    if isinstance(head, list) and head:
        row = head[0]
        return DuplicateResult(True, "exact_branch", int(row.get("number") or 0) or None, row.get("url"))

    prs = _run_json(
        ["gh", "pr", "list", "--state", "open", "--limit", "100", "--json", "number,url,title,body,headRefName,labels"],
        cwd=root,
    )
    if isinstance(prs, list):
        for row in prs:
            text = f"{row.get('title') or ''}\n{row.get('body') or ''}"
            if _explicitly_links_issue(text, issue):
                return DuplicateResult(True, "explicit_link", int(row.get("number") or 0) or None, row.get("url"))
        wanted = {f"issue:{issue}", f"issue-{issue}", f"autoops-issue:{issue}"}
        for row in prs:
            labels = _label_names(row)
            if labels.intersection(wanted):
                return DuplicateResult(True, "label", int(row.get("number") or 0) or None, row.get("url"))

    mapping_path = root / "data" / "autoops" / "issue_pr_map.json"
    try:
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        mapping = {}
    row = mapping.get(str(issue)) if isinstance(mapping, Mapping) else None
    if isinstance(row, Mapping) and row.get("pr"):
        return DuplicateResult(True, "mapping", int(row.get("pr")), row.get("url"))
    return DuplicateResult(False, "none")


def _estimate_scope(body: str, max_tokens: int) -> tuple[str, bool]:
    estimated_tokens = max(1, len(body) // 4)
    if estimated_tokens > max_tokens:
        return "large", True
    if estimated_tokens > max_tokens * 0.5:
        return "medium", False
    return "small", False


def _work_units(text: str) -> tuple[str, ...]:
    low = text.lower()
    units = [name for name, needles in WORK_UNIT_PATTERNS if any(needle in low for needle in needles)]
    return tuple(dict.fromkeys(units))


def precheck(issue_payload: Mapping[str, Any], *, root: Path, branch: str) -> PrecheckResult:
    issue = int(issue_payload.get("number") or 0)
    title = str(issue_payload.get("title") or "")
    body = str(issue_payload.get("body") or "")
    labels = _label_names(issue_payload)
    combined = f"{title}\n{body}"
    low = combined.lower()
    config = _load_autoops_config(root)
    max_tokens = int(config.get("codex_max_issue_tokens") or 50000)
    require_decomposition = bool(config.get("codex_large_issue_requires_decomposition", True))
    scope, too_large = _estimate_scope(combined, max_tokens)
    dup = duplicate_check(issue, branch, root=root)
    if dup.duplicate:
        return PrecheckResult(
            "duplicate_of_open_pr",
            False,
            f"owned_by_open_pr_{dup.pr}",
            scope,
            label="duplicate",
            duplicate=dup,
        )
    has_traceback = "traceback" in low and "no traceback" not in low
    has_exception = "exception" in low and "no exception" not in low
    if "market-data-noise" in labels or ("bad_quote" in low and not has_traceback and not has_exception):
        return PrecheckResult(
            "market_data_noise",
            False,
            "low_severity_market_data_noise",
            scope,
            label="stale-not-reproduced",
            duplicate=dup,
        )
    if "resolved-by-existing-fix" in labels or "already fixed" in low or "fixed on main" in low:
        return PrecheckResult(
            "already_fixed_on_main",
            False,
            "issue_evidence_indicates_existing_fix",
            scope,
            label="resolved-by-existing-fix",
            close=True,
            duplicate=dup,
        )
    if "stale" in low or "not reproduced" in low or "no recurrence" in low:
        return PrecheckResult(
            "stale_not_reproduced",
            False,
            "failure_not_reproduced_after_latest_evidence",
            scope,
            label="stale-not-reproduced",
            close=True,
            duplicate=dup,
        )
    units = _work_units(combined)
    if too_large and require_decomposition:
        return PrecheckResult(
            "needs_human_review",
            False,
            "large_issue_requires_decomposition",
            "large",
            label="needs-human-review",
            duplicate=dup,
            work_units=units,
        )
    classification = "active_reproducible" if any(token in low for token in ("traceback", "nameerror", "exception", "failed")) else "actionable"
    return PrecheckResult(classification, True, "actionable_current_issue", scope, duplicate=dup, work_units=units)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-json", type=Path, required=True)
    parser.add_argument("--issue", type=int, required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    payload = json.loads(args.issue_json.read_text(encoding="utf-8"))
    payload.setdefault("number", args.issue)
    result = precheck(payload, root=args.repo_root, branch=args.branch)
    dup = result.duplicate
    print(
        "CODEX_DUPLICATE_CHECK issue=%d result=%s source=%s pr=%s"
        % (args.issue, "duplicate" if dup.duplicate else "not_duplicate", dup.source, dup.pr if dup.pr else "none")
    )
    print(
        "CODEX_PRECHECK issue=%d classification=%s codex_required=%s reason=%s estimated_scope=%s"
        % (
            args.issue,
            result.classification,
            str(result.codex_required).lower(),
            result.reason,
            result.estimated_scope,
        )
    )
    if result.label:
        print(f"CODEX_PRECHECK_LABEL label={result.label}")
    if result.close:
        print("CODEX_PRECHECK_CLOSE close=true")
    for unit in result.work_units:
        print(f"CODEX_WORK_UNIT issue={args.issue} unit={unit}")
    return 0 if result.codex_required else 10


if __name__ == "__main__":
    raise SystemExit(main())
