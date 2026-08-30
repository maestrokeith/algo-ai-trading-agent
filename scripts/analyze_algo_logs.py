#!/usr/bin/env python3
"""Read-only log analyzer for Codex-routable AlgoSphere incidents."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from src.review_logs import paper_full_log_path


DEFAULT_THRESHOLDS: dict[str, int] = {
    "skip_reason": 5,
    "traceback": 1,
    "exception": 1,
    "service_restart": 2,
    "entry_eval_to_skip": 3,
    "allocator_dispatch_skip": 3,
    "unstable_quote": 5,
    "spread_too_wide": 5,
    "expected_strategy_skip": 50,
    "pipeline_skip_after_entry": 1,
}

DEFAULT_LIVE_THRESHOLDS: dict[str, int] = {
    **DEFAULT_THRESHOLDS,
    "entry_eval_to_skip": 10,
    "unstable_quote": 150,
    "spread_too_wide": 15,
}

WATCHED_SKIP_REASONS = {
    "dynamic_price_below_minimum",
    "weak_catalyst_dynamic_non_exceptional_live",
    "dynamic_weak_catalyst_price_not_above_vwap",
}

EXPECTED_STRATEGY_SKIP_REASONS = {
    "weak_catalyst_dynamic_non_exceptional_live",
    "dynamic_weak_catalyst_price_not_above_vwap",
}

EXPECTED_GUARD_SKIP_REASONS = {
    "allocator_add_on_once_per_day",
    "dynamic_relative_volume",
    "dynamic_spread_cap",
}

PIPELINE_INCONSISTENCY_REASONS = {
    "dynamic_price_below_minimum",
}

MARKET_DATA_REASONS = {
    "unstable_quote",
    "spread_too_wide",
}

HARD_ERROR_REASONS = {
    "traceback",
    "module_not_found",
    "exception",
    "service_restart_loop",
}

RESTART_PATTERNS = (
    "Scheduled restart job",
    "start request repeated too quickly",
    "Main process exited",
    "Started Algo",
    "Starting Algo",
    "algo.service: Failed",
)


@dataclass(frozen=True)
class LogFinding:
    """One actionable recurring log finding."""

    fingerprint: str
    title: str
    component: str
    reason: str
    severity: str
    count: int
    first_seen: str
    last_seen: str
    classification: str
    snippets: list[str] = field(default_factory=list)
    probable_root_cause: str = "Runtime path is repeatedly producing the same failure marker."
    suggested_investigation: str = "Inspect the referenced component and add/repair tests for the repeated marker."


@dataclass
class AnalysisResult:
    """Summary returned by one analyzer run."""

    environment: str
    findings: list[LogFinding]
    created: int = 0
    suppressed: int = 0
    duplicates: int = 0
    duration_seconds: float = 0.0
    dry_run: bool = False
    timer: bool = False


@dataclass(frozen=True)
class AnalyzeFixResult:
    """Verification result for a previously generated analyzer issue."""

    issue_number: int
    environment: str
    status: str
    fingerprint: str | None = None
    reason: str | None = None
    occurrences: int = 0
    details: str = ""


class CommandRunner:
    """Small subprocess seam for tests."""

    def run(self, args: Sequence[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(args),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=check,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(value or "").strip().lower())
    return re.sub(r"_+", "_", text).strip("_") or "unknown"


def stable_fingerprint(
    *,
    environment: str,
    component: str,
    reason: str,
    stack_trace: str | None = None,
) -> str:
    """Build a stable issue fingerprint from env, component, reason, and trace class."""
    base = f"{_safe_slug(environment)}:{_safe_slug(component)}:{_safe_slug(reason)}"
    trace = str(stack_trace or "").strip()
    if not trace:
        return f"log-analysis:{base}"
    digest = hashlib.sha1(trace.encode("utf-8")).hexdigest()[:10]
    return f"log-analysis:{base}:{digest}"


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _thresholds_for_environment(analysis: Mapping[str, Any], environment: str) -> dict[str, int]:
    thresholds = dict(DEFAULT_LIVE_THRESHOLDS if environment == "live" else DEFAULT_THRESHOLDS)
    shared = analysis.get("issue_thresholds")
    env_specific = analysis.get(f"{environment}_issue_thresholds")
    for configured in (shared, env_specific):
        if not isinstance(configured, Mapping):
            continue
        for key, value in configured.items():
            try:
                thresholds[str(key)] = max(1, int(float(value)))
            except (TypeError, ValueError):
                continue
    return thresholds


def load_analysis_config(root: Path, *, environment: str = "live") -> dict[str, Any]:
    path = root / "config" / "default.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        payload = {}
    analysis = payload.get("analysis") if isinstance(payload, Mapping) else {}
    if not isinstance(analysis, Mapping):
        analysis = {}
    processor_labels = analysis.get("processor_labels")
    if not isinstance(processor_labels, Mapping):
        processor_labels = {}
    return {
        "enabled": bool(analysis.get("enabled", True)),
        "interval_minutes": int(float(analysis.get("interval_minutes", 5) or 5)),
        "lookback_minutes": int(float(analysis.get("lookback_minutes", 30 if environment == "live" else 15) or 15)),
        "duplicate_window_hours": int(float(analysis.get("duplicate_window_hours", 24) or 24)),
        "issue_thresholds": _thresholds_for_environment(analysis, environment),
        "processor_label_enabled": _bool(analysis.get("processor_label_enabled"), default=False),
        "processor_labels": {
            "paper": str(processor_labels.get("paper") or "processor:mac-paper"),
            "live": str(processor_labels.get("live") or "processor:live"),
        },
        "github_issue_enabled": _bool(analysis.get("github_issue_enabled"), default=False),
        "github_comment_enabled": _bool(analysis.get("github_comment_enabled"), default=False),
        "auto_close_resolved_issues": _bool(analysis.get("auto_close_resolved_issues"), default=False),
    }


def collect_logs(
    *,
    environment: str,
    lookback_minutes: int,
    log_file: Path | None = None,
    root: Path | None = None,
    runner: CommandRunner | None = None,
) -> str:
    """Read recent logs without mutating the running service."""
    if log_file is not None:
        try:
            return log_file.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            print(
                f"LOG_ANALYZER_LOG_SOURCE source=none reason=missing_log_file path={log_file}",
                file=sys.stderr,
            )
            return ""
    if shutil.which("journalctl") is None:
        configured_log = os.environ.get("ALGO_ANALYZE_LOG_FILE")
        if environment == "paper":
            configured_log = configured_log or os.environ.get("ALGO_PAPER_LOG_FILE")
        candidates: list[Path] = []
        if configured_log:
            candidates.append(Path(configured_log))
        if environment == "paper":
            candidates.append(paper_full_log_path(root or PROJECT_ROOT))
        for candidate in candidates:
            if candidate.is_file():
                print(f"LOG_ANALYZER_LOG_SOURCE source=file path={candidate}", file=sys.stderr)
                return candidate.read_text(encoding="utf-8", errors="replace")
        hint = "set ALGO_PAPER_LOG_FILE or pass --log-file" if environment == "paper" else "pass --log-file"
        print(
            "LOG_ANALYZER_LOG_SOURCE source=none reason=journalctl_unavailable "
            f"hint={hint}",
            file=sys.stderr,
        )
        return ""
    unit = "algo.service" if environment == "live" else "paper.service"
    try:
        proc = (runner or CommandRunner()).run(
            [
                "journalctl",
                "-u",
                unit,
                "--since",
                f"{int(lookback_minutes)} min ago",
                "--no-pager",
            ]
        )
    except FileNotFoundError:
        print(
            "LOG_ANALYZER_LOG_SOURCE source=none reason=journalctl_unavailable hint=pass --log-file",
            file=sys.stderr,
        )
        return ""
    print("LOG_ANALYZER_LOG_SOURCE source=journalctl", file=sys.stderr)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _line_timestamp(line: str, fallback_index: int) -> str:
    iso = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+", line)
    if iso:
        return iso.group(0)
    syslog = re.match(r"([A-Z][a-z]{2}\s+\d{1,2}\s+[0-9:]{8})", line)
    if syslog:
        return syslog.group(1)
    return f"line:{fallback_index}"


def _symbol_from_line(line: str) -> str | None:
    match = re.search(r"\bsymbol=([A-Z0-9._-]+)\b", line)
    if match:
        return match.group(1).upper()
    return None


def _reason_from_line(line: str) -> str | None:
    match = re.search(r"\breason=([A-Za-z0-9_.:-]+)", line)
    if match:
        return match.group(1)
    if "unstable_quote" in line or "unstable quote" in line.lower():
        return "unstable_quote"
    if "spread_too_wide" in line or "spread too wide" in line.lower():
        return "spread_too_wide"
    return None


def _representative(lines: Sequence[str], limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        text = str(line).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[-500:])
        if len(out) >= limit:
            break
    return out


def is_expected_guard_skip(reason: str, line: str = "") -> bool:
    clean = str(reason or "").strip()
    low = f"{clean} {line}".lower()
    if clean in EXPECTED_GUARD_SKIP_REASONS:
        return True
    if clean.startswith("dynamic_weak_catalyst_relative_volume_below_"):
        return True
    if "spread cap" in low or "spread_cap" in low:
        return True
    if "min_notional" in low or "min notional" in low:
        return True
    if re.search(r"\bsize\s*=\s*0\b", low) or "size=0" in low:
        return True
    return False


def is_downstream_entry_evidence(line: str) -> bool:
    markers = (
        "ENTRY_TO_ALLOCATOR_TRACE",
        "ALLOCATOR ACTION",
        "ALLOCATOR_ACTION",
        "ALLOCATOR ACTIONS",
        "ORDER_INTENT",
        "ORDER_SKIP",
        "DISPATCH_SKIP",
        "ALLOCATOR_DISPATCH_SKIPPED",
        "ORDER_SUBMITTED",
        "ALLOCATOR_DISPATCH_END",
    )
    return any(marker in line for marker in markers)


def classify_reason(reason: str, *, after_entry_pass: bool = False) -> str:
    clean = str(reason or "").strip()
    if clean in HARD_ERROR_REASONS:
        return "hard_error"
    if clean in MARKET_DATA_REASONS:
        return "market_data_noise"
    if clean in EXPECTED_STRATEGY_SKIP_REASONS:
        return "expected_strategy_skip"
    if is_expected_guard_skip(clean):
        return "market_data_noise" if "spread" in clean else "expected_guard_skip"
    if clean in PIPELINE_INCONSISTENCY_REASONS and after_entry_pass:
        return "pipeline_inconsistency"
    if after_entry_pass and clean not in EXPECTED_STRATEGY_SKIP_REASONS and not is_expected_guard_skip(clean):
        return "pipeline_inconsistency"
    if clean in PIPELINE_INCONSISTENCY_REASONS:
        return "pipeline_inconsistency"
    return "pipeline_inconsistency"


def _make_finding(
    *,
    environment: str,
    component: str,
    reason: str,
    count: int,
    lines: Sequence[tuple[int, str]],
    severity: str,
    classification: str,
    title: str,
    root_cause: str,
    investigation: str,
    stack_trace: str | None = None,
) -> LogFinding:
    first_idx = lines[0][0] if lines else 0
    last_idx = lines[-1][0] if lines else 0
    return LogFinding(
        fingerprint=stable_fingerprint(
            environment=environment,
            component=component,
            reason=reason,
            stack_trace=stack_trace,
        ),
        title=title,
        component=component,
        reason=reason,
        severity=severity,
        count=count,
        first_seen=_line_timestamp(lines[0][1], first_idx) if lines else "unknown",
        last_seen=_line_timestamp(lines[-1][1], last_idx) if lines else "unknown",
        classification=classification,
        snippets=_representative([line for _idx, line in lines]),
        probable_root_cause=root_cause,
        suggested_investigation=investigation,
    )


def analyze_log_text(
    log_text: str,
    *,
    environment: str,
    thresholds: Mapping[str, int] | None = None,
) -> list[LogFinding]:
    """Detect recurring actionable issues in recent log text."""
    limits = {**DEFAULT_THRESHOLDS, **dict(thresholds or {})}
    lines = [(idx, line) for idx, line in enumerate(str(log_text or "").splitlines(), start=1)]
    findings: list[LogFinding] = []
    skip_lines: dict[str, list[tuple[int, str]]] = defaultdict(list)
    unstable: list[tuple[int, str]] = []
    spread: list[tuple[int, str]] = []
    restart: list[tuple[int, str]] = []
    traceback: list[tuple[int, str]] = []
    exceptions: list[tuple[int, str]] = []
    entry_pass_seen: dict[str, int] = {}
    entry_pass_lines: dict[str, tuple[int, str]] = {}
    downstream_seen: set[str] = set()
    allocator_seen: dict[str, int] = {}
    pass_to_skip: list[tuple[int, str]] = []
    pass_to_skip_by_reason: dict[str, list[tuple[int, str]]] = defaultdict(list)
    allocator_dispatch_skip: list[tuple[int, str]] = []

    for idx, line in lines:
        reason = _reason_from_line(line)
        if "ORDER_SKIP" in line and reason:
            skip_lines[reason].append((idx, line))
        low = line.lower()
        if reason == "unstable_quote" or "unstable quote" in low:
            unstable.append((idx, line))
        if reason == "spread_too_wide" or "spread too wide" in low:
            spread.append((idx, line))
        if "Traceback (most recent call last)" in line:
            traceback.append((idx, line))
        if (
            "ModuleNotFoundError" in line
            or re.search(r"\b(?:[A-Za-z_]*Error|Exception):", line)
            or re.search(r"\bERROR:", line)
        ):
            exceptions.append((idx, line))
        if any(pattern.lower() in low for pattern in RESTART_PATTERNS):
            restart.append((idx, line))

        symbol = _symbol_from_line(line) or "_global"
        if "ENTRY_EVAL_PASS" in line or ("ENTRY_EVAL" in line and "final=T" in line):
            entry_pass_seen[symbol] = idx
            entry_pass_lines[symbol] = (idx, line)
        elif symbol in entry_pass_seen and is_downstream_entry_evidence(line):
            downstream_seen.add(symbol)
        if "ALLOCATOR ACTION" in line or "ALLOCATOR_ACTION" in line:
            allocator_seen[symbol] = idx
        if ("ORDER_SKIP" in line or "DISPATCH_SKIP" in line or "ALLOCATOR_DISPATCH_SKIPPED" in line) and symbol in entry_pass_seen:
            skip_reason = reason or "dispatch_skip"
            if classify_reason(skip_reason, after_entry_pass=True) not in {"expected_strategy_skip", "expected_guard_skip", "market_data_noise"}:
                pass_to_skip.append((idx, line))
                pass_to_skip_by_reason[skip_reason].append((idx, line))
        if ("DISPATCH_SKIP" in line or "ORDER_SKIP" in line or "ALLOCATOR_DISPATCH_SKIPPED" in line) and symbol in allocator_seen:
            skip_reason = reason or "dispatch_skip"
            if classify_reason(skip_reason, after_entry_pass=True) not in {"expected_strategy_skip", "expected_guard_skip", "market_data_noise"}:
                allocator_dispatch_skip.append((idx, line))

    for reason, grouped in sorted(skip_lines.items()):
        if reason not in WATCHED_SKIP_REASONS:
            continue
        classification = classify_reason(reason)
        threshold_key = "expected_strategy_skip" if classification == "expected_strategy_skip" else "skip_reason"
        if len(grouped) >= int(limits[threshold_key]):
            findings.append(
                _make_finding(
                    environment=environment,
                    component="execution",
                    reason=reason,
                    count=len(grouped),
                    lines=grouped,
                    severity="low" if classification == "expected_strategy_skip" else "medium",
                    classification=classification,
                    title=f"[{environment.upper()}] Repeated ORDER_SKIP reason={reason}",
                    root_cause=f"Execution repeatedly skipped orders with reason={reason}.",
                    investigation=(
                        "Review whether repeated expected strategy skips are concentrated in one lane/session."
                        if classification == "expected_strategy_skip"
                        else "Compare scanner, entry, allocator, and execution metadata for the sampled symbols."
                    ),
                )
            )
    for reason in sorted(PIPELINE_INCONSISTENCY_REASONS):
        grouped = pass_to_skip_by_reason.get(reason, [])
        if len(grouped) >= int(limits["pipeline_skip_after_entry"]):
            findings.append(
                _make_finding(
                    environment=environment,
                    component="entry_to_execution",
                    reason=f"{reason}_after_entry_pass",
                    count=len(grouped),
                    lines=grouped,
                    severity="medium",
                    classification="pipeline_inconsistency",
                    title=f"[{environment.upper()}] {reason} after ENTRY_EVAL_PASS",
                    root_cause=f"Entry approved symbols that execution later skipped with reason={reason}.",
                    investigation="Align scanner, entry, allocator, and execution metadata so the earlier lane rejects the same condition.",
                )
            )
    silent_entry_passes = [
        row
        for symbol, row in sorted(entry_pass_lines.items())
        if symbol not in downstream_seen
    ]
    if len(silent_entry_passes) >= int(limits["pipeline_skip_after_entry"]):
        findings.append(
            _make_finding(
                environment=environment,
                component="entry_to_execution",
                reason="entry_eval_pass_to_dispatch_skip",
                count=len(silent_entry_passes),
                lines=silent_entry_passes,
                severity="medium",
                classification="pipeline_inconsistency",
                title=f"[{environment.upper()}] ENTRY_EVAL_PASS without downstream allocator or dispatch evidence",
                root_cause="Symbols passed entry evaluation but no allocator, dispatch, order, or terminal skip marker appeared in the analysis window.",
                investigation="Trace entry-to-allocator handoff logs for the sampled symbols and ensure every entry pass has a terminal downstream marker.",
            )
        )
    if len(unstable) >= int(limits["unstable_quote"]):
        findings.append(
            _make_finding(
                environment=environment,
                component="market_data",
                reason="unstable_quote",
                count=len(unstable),
                lines=unstable,
                severity="low",
                classification="market_data_noise",
                title=f"[{environment.upper()}] Repeated unstable quote blocks",
                root_cause="Recent quotes are repeatedly unstable or crossed.",
                investigation="Check quote source freshness, spread caps, and whether symbols are illiquid.",
            )
        )
    if len(spread) >= int(limits["spread_too_wide"]):
        findings.append(
            _make_finding(
                environment=environment,
                component="market_data",
                reason="spread_too_wide",
                count=len(spread),
                lines=spread,
                severity="low",
                classification="market_data_noise",
                title=f"[{environment.upper()}] Repeated spread_too_wide blocks",
                root_cause="Bid/ask spread is repeatedly wider than configured execution limits.",
                investigation="Check spread diagnostics and whether affected symbols should be filtered earlier.",
            )
        )
    if len(traceback) >= int(limits["traceback"]):
        trace_text = "\n".join(line for _idx, line in lines[max(0, traceback[0][0] - 1) : traceback[0][0] + 8])
        findings.append(
            _make_finding(
                environment=environment,
                component="runtime",
                reason="traceback",
                count=len(traceback),
                lines=traceback,
                severity="high",
                classification="hard_error",
                title=f"[{environment.upper()}] Runtime traceback in algo logs",
                root_cause="The process emitted a Python traceback.",
                investigation="Use the stack trace to reproduce the failing code path and add a regression test.",
                stack_trace=trace_text,
            )
        )
    if len(exceptions) >= int(limits["exception"]):
        reason = "module_not_found" if any("ModuleNotFoundError" in line for _idx, line in exceptions) else "exception"
        findings.append(
            _make_finding(
                environment=environment,
                component="runtime",
                reason=reason,
                count=len(exceptions),
                lines=exceptions,
                severity="high",
                classification="hard_error",
                title=f"[{environment.upper()}] Runtime exception marker in algo logs",
                root_cause="The process emitted exception/error markers.",
                investigation="Inspect the sampled exception lines and add a focused regression test.",
                stack_trace="\n".join(line for _idx, line in exceptions[:5]),
            )
        )
    if len(restart) >= int(limits["service_restart"]):
        findings.append(
            _make_finding(
                environment=environment,
                component="service",
                reason="service_restart_loop",
                count=len(restart),
                lines=restart,
                severity="high",
                classification="hard_error",
                title=f"[{environment.upper()}] Possible algo service restart loop",
                root_cause="systemd restart/start markers repeated inside the analysis window.",
                investigation="Inspect service status, journal around the first restart, and any preceding exception.",
            )
        )
    if len(pass_to_skip) >= int(limits["entry_eval_to_skip"]):
        findings.append(
            _make_finding(
                environment=environment,
                component="entry_to_execution",
                reason="entry_eval_pass_to_dispatch_skip",
                count=len(pass_to_skip),
                lines=pass_to_skip,
                severity="medium",
                classification="pipeline_inconsistency",
                title=f"[{environment.upper()}] ENTRY_EVAL_PASS followed by dispatch/order skips",
                root_cause="Symbols that passed entry evaluation later skipped at dispatch/execution.",
                investigation="Compare entry approval metadata against allocator and execution guard reasons.",
            )
        )
    if len(allocator_dispatch_skip) >= int(limits["allocator_dispatch_skip"]):
        findings.append(
            _make_finding(
                environment=environment,
                component="allocator",
                reason="allocator_action_to_dispatch_skip",
                count=len(allocator_dispatch_skip),
                lines=allocator_dispatch_skip,
                severity="medium",
                classification="pipeline_inconsistency",
                title=f"[{environment.upper()}] Allocator actions followed by dispatch skips",
                root_cause="Allocator accepted actions that later failed dispatch/execution gates.",
                investigation="Validate allocator action payloads include the fields required by execution guards.",
            )
        )
    return findings


def issue_labels(
    environment: str,
    severity: str,
    *,
    processor_label_enabled: bool = False,
    processor_labels: Mapping[str, str] | None = None,
) -> list[str]:
    labels = [
        "codex",
        "auto-fix",
        "algo-failure",
        f"environment:{environment}",
        f"severity:{severity}",
    ]
    configured = processor_labels or {}
    default_processors = {"paper": "processor:mac-paper", "live": "processor:live-linux"}
    processor = str(configured.get(environment) or default_processors.get(environment, "")).strip()
    if processor and (processor_label_enabled or environment == "paper"):
        labels.append(processor)
    return labels


def issue_label_severity(finding: LogFinding, *, environment: str) -> str:
    """Return the severity label used for analyzer-created GitHub issues."""
    if environment == "paper":
        return "high" if finding.count >= 50 else "medium"
    return finding.severity


def available_github_labels(runner: CommandRunner) -> set[str] | None:
    result = runner.run(["gh", "label", "list", "--json", "name", "--limit", "200"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    labels = {str(row.get("name") or "").strip() for row in payload if isinstance(row, Mapping)}
    return {label for label in labels if label}


def validated_issue_labels(labels: Sequence[str], *, runner: CommandRunner) -> list[str]:
    available = available_github_labels(runner)
    if available is None:
        return list(labels)
    return [label for label in labels if label in available]


def build_issue_body(finding: LogFinding, *, environment: str, lookback_minutes: int) -> str:
    snippets = "\n".join(finding.snippets)
    return (
        "Automated log analyzer detected a recurring AlgoSphere runtime problem.\n\n"
        f"- Environment: {environment.upper()}\n"
        f"- environment={environment}\n"
        f"- Analysis window: last {lookback_minutes} minutes\n"
        f"- First seen: {finding.first_seen}\n"
        f"- Last seen: {finding.last_seen}\n"
        f"- Count: {finding.count}\n"
        f"- Component: {finding.component}\n"
        f"- Reason: {finding.reason}\n"
        f"- Severity: {finding.severity}\n"
        f"- Classification: {finding.classification}\n"
        f"- Fingerprint: {finding.fingerprint}\n\n"
        "## Evidence\n\n"
        "```text\n"
        f"{snippets}\n"
        "```\n\n"
        "## Probable Root Cause\n\n"
        f"{finding.probable_root_cause}\n\n"
        "## Suggested Investigation\n\n"
        f"{finding.suggested_investigation}\n\n"
        "## Codex Scope\n\n"
        "- Keep the analyzer and fix read-only unless a separate issue explicitly asks for trading changes.\n"
        "- Do not loosen strategy thresholds or execution safety gates.\n"
        "- Add or update focused regression tests for the detected path.\n"
        "\n## Codex instructions\n\n"
        "- git pull --rebase\n"
        "- investigate root cause\n"
        "- add regression test\n"
        "- run targeted tests\n"
        "- git push\n"
    )


def load_fingerprint_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"fingerprints": {}}
    return payload if isinstance(payload, dict) else {"fingerprints": {}}


def save_fingerprint_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def local_duplicate_active(
    state: Mapping[str, Any],
    fingerprint: str,
    *,
    duplicate_window_hours: int,
    now: datetime | None = None,
) -> bool:
    rows = state.get("fingerprints") if isinstance(state, Mapping) else {}
    row = rows.get(fingerprint) if isinstance(rows, Mapping) else None
    if not isinstance(row, Mapping):
        return False
    last = _parse_iso(str(row.get("last_seen") or ""))
    if last is None:
        return False
    current = now or datetime.now(timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (current - last).total_seconds() < float(duplicate_window_hours) * 3600.0


def record_fingerprint(
    state: dict[str, Any],
    finding: LogFinding,
    *,
    action: str,
    issue_number: int | None = None,
) -> None:
    rows = state.setdefault("fingerprints", {})
    existing = rows.get(finding.fingerprint) if isinstance(rows, dict) else None
    count = int(existing.get("seen_count", 0)) + 1 if isinstance(existing, Mapping) else 1
    rows[finding.fingerprint] = {
        "last_seen": _now_iso(),
        "seen_count": count,
        "action": action,
        "issue_number": issue_number,
        "title": finding.title,
        "reason": finding.reason,
        "component": finding.component,
        "severity": finding.severity,
    }


def _issue_matches_environment(issue: Mapping[str, Any], environment: str) -> bool:
    labels = issue_label_names(issue)
    body = str(issue.get("body") or "")
    return f"environment:{environment}" in labels or f"environment={environment}" in body or f"Environment: {environment.upper()}" in body


def _issue_matches_finding(issue: Mapping[str, Any], finding: LogFinding, *, environment: str) -> bool:
    title = str(issue.get("title") or "")
    body = str(issue.get("body") or "")
    if not _issue_matches_environment(issue, environment):
        return False
    if finding.fingerprint and f"Fingerprint: {finding.fingerprint}" in body:
        return True
    if finding.fingerprint and finding.fingerprint in body:
        return True
    component_match = f"Component: {finding.component}" in body
    reason_match = f"Reason: {finding.reason}" in body or f"reason={finding.reason}" in body
    if component_match and reason_match:
        return True
    return bool(title and title == finding.title)


def find_open_duplicate_issue(runner: CommandRunner, finding: LogFinding, *, environment: str) -> int | None:
    search_terms = [finding.fingerprint]
    if finding.title not in search_terms:
        search_terms.append(finding.title)
    seen_numbers: set[int] = set()
    for search in search_terms:
        result = runner.run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--search",
                search,
                "--json",
                "number,title,body,labels",
                "--limit",
                "50",
            ]
        )
        if result.returncode != 0:
            continue
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            continue
        for issue in payload:
            if not isinstance(issue, Mapping):
                continue
            try:
                number = int(issue["number"])
            except (KeyError, TypeError, ValueError):
                continue
            if number in seen_numbers:
                continue
            seen_numbers.add(number)
            if _issue_matches_finding(issue, finding, environment=environment):
                return number
    return None


def find_open_issue(runner: CommandRunner, fingerprint: str) -> int | None:
    result = runner.run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--search",
            fingerprint,
            "--json",
            "number,title,body",
            "--limit",
            "20",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for issue in payload:
        text = f"{issue.get('title', '')}\n{issue.get('body', '')}"
        if fingerprint in text:
            try:
                return int(issue["number"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def duplicate_comment_body(finding: LogFinding, *, environment: str, lookback_minutes: int) -> str:
    snippets = "\n".join(finding.snippets[-10:])
    return (
        "Log analyzer saw the same open issue fingerprint again.\n\n"
        f"- Environment: {environment.upper()}\n"
        f"- Fingerprint: {finding.fingerprint}\n"
        f"- Component: {finding.component}\n"
        f"- Reason: {finding.reason}\n"
        f"- Count: {finding.count}\n"
        f"- Window: last {lookback_minutes} minutes\n\n"
        "Latest evidence:\n"
        "```text\n"
        f"{snippets}\n"
        "```\n"
    )


def create_github_issue(
    runner: CommandRunner,
    finding: LogFinding,
    *,
    environment: str,
    lookback_minutes: int,
    processor_label_enabled: bool = False,
    processor_labels: Mapping[str, str] | None = None,
    dry_run: bool,
) -> int | None:
    body = build_issue_body(finding, environment=environment, lookback_minutes=lookback_minutes)
    label_severity = issue_label_severity(finding, environment=environment)
    labels = issue_labels(
        environment,
        label_severity,
        processor_label_enabled=processor_label_enabled,
        processor_labels=processor_labels,
    )
    labels = validated_issue_labels(labels, runner=runner) if not dry_run else labels
    if dry_run:
        print(f"LOG_ANALYZER_DRY_RUN_ISSUE title={finding.title!r} labels={','.join(labels)}")
        print(f"LOG_ANALYZER_DRY_RUN_FINGERPRINT {finding.fingerprint}")
        print(
            "ISSUE_ROUTING env=%s classification=%s severity=%s labels=%s fingerprint=%s"
            % (environment, finding.classification, label_severity, ",".join(labels), finding.fingerprint)
        )
        return None
    duplicate = find_open_duplicate_issue(runner, finding, environment=environment)
    if duplicate is not None:
        print(
            "ISSUE_DUPLICATE existing_issue=%s fingerprint=%s"
            % (duplicate, finding.fingerprint)
        )
        return duplicate
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    try:
        args = ["gh", "issue", "create", "--title", finding.title, "--body-file", body_path]
        for label in labels:
            args.extend(["--label", label])
        result = runner.run(args)
    finally:
        Path(body_path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "gh issue create failed").strip())
    match = re.search(r"/issues/(\d+)", result.stdout or "")
    issue_number = int(match.group(1)) if match else None
    print(
        "ISSUE_CREATED env=%s issue=%s classification=%s severity=%s fingerprint=%s labels=%s"
        % (environment, issue_number or "unknown", finding.classification, label_severity, finding.fingerprint, ",".join(labels))
    )
    return issue_number


def process_findings(
    findings: Sequence[LogFinding],
    *,
    environment: str,
    lookback_minutes: int,
    duplicate_window_hours: int,
    state_path: Path,
    runner: CommandRunner,
    dry_run: bool,
    timer: bool = False,
    processor_label_enabled: bool = False,
    processor_labels: Mapping[str, str] | None = None,
    github_comment_enabled: bool = False,
) -> tuple[int, int, int]:
    state = load_fingerprint_state(state_path)
    created = suppressed = duplicates = 0
    for finding in findings:
        if timer and finding.classification == "market_data_noise" and finding.severity == "low":
            suppressed += 1
            record_fingerprint(state, finding, action="timer_market_data_noise_suppressed")
            print(
                "ISSUE_SUPPRESSED env=%s reason=timer_market_data_noise fingerprint=%s"
                % (environment, finding.fingerprint)
            )
            continue
        if finding.classification == "market_data_noise" and local_duplicate_active(
            state,
            finding.fingerprint,
            duplicate_window_hours=duplicate_window_hours,
        ):
            suppressed += 1
            record_fingerprint(state, finding, action="local_duplicate_suppressed")
            print(
                "ISSUE_SUPPRESSED env=%s reason=local_duplicate fingerprint=%s"
                % (environment, finding.fingerprint)
            )
            continue
        duplicate_issue = None if dry_run else find_open_duplicate_issue(runner, finding, environment=environment)
        if duplicate_issue is not None:
            duplicates += 1
            record_fingerprint(state, finding, action="github_duplicate", issue_number=duplicate_issue)
            print(
                "ISSUE_DUPLICATE existing_issue=%s fingerprint=%s"
                % (duplicate_issue, finding.fingerprint)
            )
            if github_comment_enabled:
                comment_on_issue(
                    runner,
                    duplicate_issue,
                    duplicate_comment_body(finding, environment=environment, lookback_minutes=lookback_minutes),
                )
            continue
        print(
            "ISSUE_ROUTING env=%s classification=%s fingerprint=%s"
            % (environment, finding.classification, finding.fingerprint)
        )
        print(
            "CODEX_ELIGIBLE env=%s classification=%s fingerprint=%s"
            % (environment, finding.classification, finding.fingerprint)
        )
        issue_number = create_github_issue(
            runner,
            finding,
            environment=environment,
            lookback_minutes=lookback_minutes,
            processor_label_enabled=processor_label_enabled,
            processor_labels=processor_labels,
            dry_run=dry_run,
        )
        created += 0 if dry_run else 1
        record_fingerprint(state, finding, action="dry_run" if dry_run else "created", issue_number=issue_number)
    save_fingerprint_state(state_path, state)
    return created, suppressed, duplicates


def run_analysis(
    *,
    root: Path,
    environment: str,
    log_file: Path | None = None,
    lookback_minutes: int | None = None,
    duplicate_window_hours: int | None = None,
    dry_run: bool = False,
    runner: CommandRunner | None = None,
    state_path: Path | None = None,
    timer: bool = False,
) -> AnalysisResult:
    started = time.perf_counter()
    cfg = load_analysis_config(root, environment=environment)
    if not bool(cfg["enabled"]):
        return AnalysisResult(
            environment=environment,
            findings=[],
            duration_seconds=time.perf_counter() - started,
            dry_run=dry_run,
        )
    lookback = int(lookback_minutes or cfg["lookback_minutes"])
    if timer and environment == "live":
        lookback = max(30, lookback)
    duplicate_window = int(duplicate_window_hours or cfg["duplicate_window_hours"])
    thresholds = dict(cfg["issue_thresholds"])
    if timer and environment == "live":
        thresholds["unstable_quote"] = max(int(thresholds.get("unstable_quote", 0)), 150)
        thresholds["spread_too_wide"] = max(int(thresholds.get("spread_too_wide", 0)), 15)
        thresholds["entry_eval_to_skip"] = max(int(thresholds.get("entry_eval_to_skip", 0)), 10)
    logs = collect_logs(
        environment=environment,
        lookback_minutes=lookback,
        log_file=log_file,
        root=root,
        runner=runner,
    )
    findings = analyze_log_text(logs, environment=environment, thresholds=thresholds)
    create_issues = bool(cfg["github_issue_enabled"]) and not dry_run
    created, suppressed, duplicates = process_findings(
        findings,
        environment=environment,
        lookback_minutes=lookback,
        duplicate_window_hours=duplicate_window,
        state_path=state_path or root / "data" / "log_analysis" / f"{environment}_fingerprints.json",
        runner=runner or CommandRunner(),
        dry_run=not create_issues,
        timer=timer,
        processor_label_enabled=bool(cfg["processor_label_enabled"]),
        processor_labels=cfg["processor_labels"],
        github_comment_enabled=bool(cfg["github_comment_enabled"]),
    )
    return AnalysisResult(
        environment=environment,
        findings=findings,
        created=created,
        suppressed=suppressed,
        duplicates=duplicates,
        duration_seconds=time.perf_counter() - started,
        dry_run=dry_run,
        timer=timer,
    )


def format_summary(result: AnalysisResult, *, compact: bool = False) -> list[str]:
    if compact:
        return [
            "LOG_ANALYZER_TIMER env=%s detected=%d suppressed=%d duplicates=%d created=%d duration=%.3fs"
            % (
                result.environment,
                len(result.findings),
                result.suppressed,
                result.duplicates,
                result.created,
                result.duration_seconds,
            )
        ]
    return [
        f"LOG_ANALYZER env={result.environment} dry_run={str(result.dry_run).lower()}",
        f"issues detected={len(result.findings)}",
        f"issues suppressed={result.suppressed}",
        f"duplicates ignored={result.duplicates}",
        f"GitHub issue created={result.created}",
        f"analysis duration={result.duration_seconds:.3f}s",
    ]


def issue_view(runner: CommandRunner, issue_number: int) -> Mapping[str, Any] | None:
    result = runner.run(["gh", "issue", "view", str(issue_number), "--json", "number,title,body,labels"])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def extract_fingerprint(text: str) -> str | None:
    match = re.search(r"Fingerprint:\s*(`?)([A-Za-z0-9_.:-]+)\1", text or "")
    return match.group(2) if match else None


def extract_reason_from_issue(text: str) -> str | None:
    match = re.search(r"Reason:\s*([A-Za-z0-9_.:-]+)", text or "")
    if match:
        return match.group(1)
    match = re.search(r"reason=([A-Za-z0-9_.:-]+)", text or "")
    return match.group(1) if match else None


def issue_label_names(issue: Mapping[str, Any]) -> set[str]:
    labels = issue.get("labels")
    if not isinstance(labels, Sequence) or isinstance(labels, (str, bytes)):
        return set()
    names: set[str] = set()
    for label in labels:
        if isinstance(label, Mapping):
            name = str(label.get("name") or "").strip()
        else:
            name = str(label or "").strip()
        if name:
            names.add(name)
    return names


def is_matching_analyzer_issue(
    issue: Mapping[str, Any],
    *,
    environment: str,
    fingerprint: str | None,
) -> bool:
    body = str(issue.get("body") or "")
    labels = issue_label_names(issue)
    if "Automated log analyzer detected" not in body:
        return False
    if fingerprint and fingerprint not in body:
        return False
    if f"environment={environment}" not in body and f"Environment: {environment.upper()}" not in body:
        return False
    return "algo-failure" in labels and f"environment:{environment}" in labels


def _symbols_from_text(text: str) -> set[str]:
    return {match.group(1).upper() for match in re.finditer(r"\bsymbol=([A-Z0-9._-]+)\b", text or "")}


def successful_downstream_evidence(logs: str, *, issue_body: str) -> list[str]:
    """Return representative lines proving entry/allocator reached order submission."""
    wanted_symbols = _symbols_from_text(issue_body)
    by_symbol: dict[str, dict[str, str]] = defaultdict(dict)
    for line in str(logs or "").splitlines():
        symbol = _symbol_from_line(line)
        if not symbol:
            continue
        if wanted_symbols and symbol not in wanted_symbols:
            continue
        stages = by_symbol[symbol]
        if "ENTRY_EVAL_PASS" in line or ("ENTRY_EVAL" in line and "final=T" in line):
            stages.setdefault("entry", line.strip())
        if "ALLOCATOR ACTION" in line or "ALLOCATOR_ACTION" in line or "ALLOCATOR ACTIONS" in line:
            stages.setdefault("allocator", line.strip())
        if "ORDER_INTENT" in line:
            stages.setdefault("intent", line.strip())
        if "ORDER_SUBMITTED" in line:
            stages.setdefault("submitted", line.strip())
        if "ALLOCATOR_DISPATCH_END" in line and "result=submitted" in line:
            stages.setdefault("dispatch_end", line.strip())
        if {"entry", "allocator", "intent", "submitted", "dispatch_end"}.issubset(stages):
            return [
                stages["entry"],
                stages["allocator"],
                stages["intent"],
                stages["submitted"],
                stages["dispatch_end"],
            ]
    return []


def requires_downstream_resolution(reason: str | None) -> bool:
    if not reason:
        return False
    return reason in {"entry_eval_pass_to_dispatch_skip", "allocator_action_to_dispatch_skip"} or reason.endswith("_after_entry_pass")


def comment_on_issue(runner: CommandRunner, issue_number: int, body: str) -> bool:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    try:
        result = runner.run(["gh", "issue", "comment", str(issue_number), "--body-file", body_path])
    finally:
        Path(body_path).unlink(missing_ok=True)
    return result.returncode == 0


def close_issue(runner: CommandRunner, issue_number: int) -> bool:
    result = runner.run(["gh", "issue", "close", str(issue_number), "--reason", "completed"])
    return result.returncode == 0


def verify_fix(
    *,
    root: Path,
    issue_number: int,
    environment: str,
    log_file: Path | None = None,
    lookback_minutes: int | None = None,
    runner: CommandRunner | None = None,
    dry_run: bool = False,
) -> AnalyzeFixResult:
    active_runner = runner or CommandRunner()
    cfg = load_analysis_config(root, environment=environment)
    issue = issue_view(active_runner, issue_number)
    if issue is None:
        return AnalyzeFixResult(issue_number=issue_number, environment=environment, status="inconclusive", details="issue_view_failed")
    body = str(issue.get("body") or "")
    fingerprint = extract_fingerprint(body)
    reason = extract_reason_from_issue(body)
    if not fingerprint and not reason:
        return AnalyzeFixResult(issue_number=issue_number, environment=environment, status="inconclusive", details="missing_fingerprint_and_reason")
    if not is_matching_analyzer_issue(issue, environment=environment, fingerprint=fingerprint):
        return AnalyzeFixResult(
            issue_number=issue_number,
            environment=environment,
            status="inconclusive",
            fingerprint=fingerprint,
            reason=reason,
            details="issue_not_matching_analyzer_metadata",
        )
    lookback = int(lookback_minutes or cfg["lookback_minutes"])
    logs = collect_logs(
        environment=environment,
        lookback_minutes=lookback,
        log_file=log_file,
        root=root,
        runner=active_runner,
    )
    if not logs.strip():
        result = AnalyzeFixResult(
            issue_number=issue_number,
            environment=environment,
            status="inconclusive",
            fingerprint=fingerprint,
            reason=reason,
            details="no_recent_logs",
        )
    else:
        findings = analyze_log_text(logs, environment=environment, thresholds=cfg["issue_thresholds"])
        downstream_evidence = successful_downstream_evidence(logs, issue_body=body) if requires_downstream_resolution(reason) else []
        matching = [
            finding
            for finding in findings
            if (fingerprint and finding.fingerprint == fingerprint)
            or (reason and (finding.reason == reason or finding.reason.startswith(f"{reason}_")))
        ]
        if matching:
            result = AnalyzeFixResult(
                issue_number=issue_number,
                environment=environment,
                status="still_occurring",
                fingerprint=fingerprint,
                reason=reason,
                occurrences=sum(finding.count for finding in matching),
                details="matching_finding_detected",
            )
        elif reason and reason in logs:
            result = AnalyzeFixResult(
                issue_number=issue_number,
                environment=environment,
                status="still_occurring",
                fingerprint=fingerprint,
                reason=reason,
                occurrences=logs.count(reason),
                details="reason_seen_below_issue_threshold",
            )
        elif downstream_evidence:
            result = AnalyzeFixResult(
                issue_number=issue_number,
                environment=environment,
                status="resolved",
                fingerprint=fingerprint,
                reason=reason,
                details="successful_downstream_path_detected\n" + "\n".join(downstream_evidence),
            )
        elif requires_downstream_resolution(reason):
            result = AnalyzeFixResult(
                issue_number=issue_number,
                environment=environment,
                status="inconclusive",
                fingerprint=fingerprint,
                reason=reason,
                details="downstream_success_evidence_missing",
            )
        else:
            result = AnalyzeFixResult(
                issue_number=issue_number,
                environment=environment,
                status="resolved",
                fingerprint=fingerprint,
                reason=reason,
                details="fingerprint_reason_absent_from_recent_logs",
            )
    if result.status == "resolved" and bool(cfg["github_comment_enabled"]) and not dry_run:
        comment_on_issue(
            active_runner,
            issue_number,
            "Log analyzer post-fix verification: `resolved` for `%s` in `%s`.\n\n"
            "Window: last %s minutes\n\nEvidence:\n```text\n%s\n```\n"
            % (result.reason or result.fingerprint or "unknown", environment, lookback, result.details),
        )
    if result.status == "resolved" and bool(cfg["auto_close_resolved_issues"]) and not dry_run:
        close_issue(active_runner, issue_number)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    raw = list(argv or sys.argv[1:])
    if raw and raw[0] == "fix":
        parser = argparse.ArgumentParser(description="Verify whether a log analyzer issue is still present")
        parser.set_defaults(command="fix")
        parser.add_argument("--issue", type=int, required=True)
        env = parser.add_mutually_exclusive_group()
        env.add_argument("--live", action="store_true", help="Analyze live algo.service logs")
        env.add_argument("--paper", action="store_true", help="Analyze paper.service logs")
        parser.add_argument("--log-file", type=Path, default=None, help="Read this log file instead of journalctl")
        parser.add_argument("--lookback-minutes", "--window-minutes", dest="lookback_minutes", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true", help="Verify only; do not comment or close issues")
        parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
        return parser.parse_args(raw[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.set_defaults(command="analyze")
    env = parser.add_mutually_exclusive_group()
    env.add_argument("--live", action="store_true", help="Analyze live algo.service logs")
    env.add_argument("--paper", action="store_true", help="Analyze paper.service logs")
    parser.add_argument("--log-file", type=Path, default=None, help="Read this log file instead of journalctl")
    parser.add_argument("--lookback-minutes", "--window-minutes", dest="lookback_minutes", type=int, default=None)
    parser.add_argument("--duplicate-window-hours", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Do not create GitHub issues")
    parser.add_argument("--timer", action="store_true", help="Timer-safe mode with stricter live noise controls")
    parser.add_argument("--json", action="store_true", help="Print machine-readable summary")
    return parser.parse_args(raw)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    environment = "paper" if args.paper else "live"
    if args.command == "fix":
        result = verify_fix(
            root=PROJECT_ROOT,
            issue_number=args.issue,
            environment=environment,
            log_file=args.log_file,
            lookback_minutes=args.lookback_minutes,
            dry_run=args.dry_run,
        )
        if args.json:
            print(json.dumps(result.__dict__, indent=2, sort_keys=True))
        else:
            print(
                "LOG_ANALYZER_FIX issue=%d env=%s status=%s reason=%s occurrences=%d details=%s"
                % (
                    result.issue_number,
                    result.environment,
                    result.status,
                    result.reason or "unknown",
                    result.occurrences,
                    result.details,
                )
            )
        return 0
    result = run_analysis(
        root=PROJECT_ROOT,
        environment=environment,
        log_file=args.log_file,
        lookback_minutes=args.lookback_minutes,
        duplicate_window_hours=args.duplicate_window_hours,
        dry_run=args.dry_run,
        timer=args.timer,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "environment": result.environment,
                    "findings": [finding.__dict__ for finding in result.findings],
                    "issues_detected": len(result.findings),
                    "issues_suppressed": result.suppressed,
                    "duplicates_ignored": result.duplicates,
                    "github_issues_created": result.created,
                    "analysis_duration_seconds": result.duration_seconds,
                    "dry_run": result.dry_run,
                    "timer": result.timer,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for line in format_summary(result, compact=args.timer):
            print(line)
        if not args.timer:
            for finding in result.findings:
                print(
                    "LOG_ANALYZER_FINDING fingerprint=%s classification=%s count=%d title=%s"
                    % (finding.fingerprint, finding.classification, finding.count, finding.title)
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
