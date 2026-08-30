#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("ALGO_REPO_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))

from src.review_logs import paper_full_log_path, paper_review_dir

SECRET_PATTERNS = (
    re.compile(r"(ALPACA(?:_LIVE)?_[A-Z0-9_]*=)[^\s]+", re.IGNORECASE),
    re.compile(r"(APCA_API_[A-Z0-9_]*=)[^\s]+", re.IGNORECASE),
    re.compile(r"(OPENAI_API_KEY=)[^\s]+", re.IGNORECASE),
    re.compile(r"(GITHUB_TOKEN=)[^\s]+", re.IGNORECASE),
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class FailureEvidence:
    short_failure: str
    expected_flow: str
    actual_missing_step: str
    grep_command: str
    matching_logs: tuple[str, ...]
    fingerprint: str
    source_path: str | None = None


@dataclass(frozen=True)
class CodexRouteResult:
    status: str
    reason: str | None = None
    issue_number: int | None = None


@dataclass(frozen=True)
class DynamicEntryFlowStatus:
    status: str
    symbols: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    states: tuple[str, ...] = ()


CRITICAL_MISSING_FLOW_FAILURES = {
    "missing_terminal_state",
    "entry eval pass without allocator trace",
    "allocator dispatch without action or reject reasons",
    "submitted order without status confirmation",
}

DYNAMIC_ENTRY_RECOVERY_MARKERS = (
    "DYNAMIC_ENTRY_EVAL_START",
    "ENTRY_EVAL_PASS",
    "ENTRY_TO_ALLOCATOR_TRACE",
    "ALLOCATOR ACTIONS",
    "ALLOCATOR_ACTION",
    "ORDER_SUBMITTED",
)

DYNAMIC_ENTRY_BUSINESS_SKIP_REASONS = (
    "cooldown",
    "atr",
    "spread",
    "rank",
    "top_n",
    "top-n",
    "cap",
    "cap_reached",
    "max_positions",
    "max_position",
    "exposure",
    "buying_power",
    "price",
    "rvol",
    "relative_volume",
    "vwap",
    "alignment",
    "range",
    "quote",
    "no_quote",
    "blocked_after_no_quote",
    "unstable_quote",
    "not_bearish_regime",
    "short_history",
    "dynamic_vwap_extension",
    "market_closed",
    "account_state_changed",
    "weak_catalyst_filter",
    "weak_catalyst",
    "allocator_rejected",
    "dispatch_rejected",
    "entry_eval_rejected",
)

VALID_DYNAMIC_TERMINAL_STATES = {
    "entry_eval_completed",
    "entry_eval_rejected",
    "short_history",
    "spread_too_wide",
    "unstable_quote",
    "cooldown",
    "weak_catalyst_filter",
    "allocator_rejected",
    "dispatch_rejected",
    "order_submitted",
    "market_closed",
    "account_state_changed",
    "dynamic_vwap_extension",
}

TERMINAL_STATE_PRIORITY = {
    "entry_eval_completed": 10,
    "entry_eval_rejected": 20,
    "short_history": 30,
    "spread_too_wide": 30,
    "unstable_quote": 30,
    "cooldown": 30,
    "weak_catalyst_filter": 30,
    "market_closed": 30,
    "account_state_changed": 30,
    "dynamic_vwap_extension": 30,
    "allocator_rejected": 40,
    "dispatch_rejected": 50,
    "order_submitted": 60,
}

DYNAMIC_ENTRY_GRACE_SECONDS = int(os.environ.get("ALGO_SELF_HEAL_DYNAMIC_ENTRY_GRACE_SECONDS", "180"))

REQUIRED_GITHUB_LABELS = {
    "codex": ("0366d6", "Issue can be routed to Codex automation."),
    "auto-fix": ("0e8a16", "Automation may attempt a code fix."),
    "algo-health": ("d93f0b", "Algo health or trading-flow regression."),
    "LIVE": ("b60205", "Live environment issue."),
    "PAPER": ("c5def5", "Paper environment issue."),
    "environment:live": ("b60205", "Live trading environment."),
    "environment:paper": ("c5def5", "Paper trading environment."),
    "processor:live-linux": ("5319e7", "Live Linux Codex processor."),
    "processor:mac-paper": ("5319e7", "Routed to macOS paper processor."),
}


class CommandRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path = ROOT,
        check: bool = False,
    ) -> CommandResult:
        proc = subprocess.run(
            list(args),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if check and result.returncode != 0:
            raise RuntimeError(f"command failed rc={result.returncode}: {' '.join(args)}\n{result.stderr}")
        return result


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[redacted]", redacted)
    return redacted


def env_upper(env: str) -> str:
    normalized = env.strip().upper()
    if normalized not in {"LIVE", "PAPER"}:
        raise ValueError(f"unsupported environment: {env}")
    return normalized


def env_lower(env: str) -> str:
    return env_upper(env).lower()


def processor_label(env: str) -> str:
    return "processor:live-linux" if env_upper(env) == "LIVE" else "processor:mac-paper"


def service_name(env: str) -> str:
    if env_upper(env) == "LIVE":
        return os.environ.get("ALGO_LIVE_SERVICE", "algo.service")
    return os.environ.get("ALGO_PAPER_SERVICE", "paper.service")


def platform_name() -> str:
    override = os.environ.get("ALGO_AUTOOPS_PLATFORM")
    if override:
        return override.strip() or platform.system()
    return platform.system()


def _read_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _latest_existing_file(paths: Sequence[Path]) -> Path | None:
    candidates = [path for path in paths if path.is_file()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: item.stat().st_mtime)[-1]


def _paper_file_log_candidates() -> list[Path]:
    configured = os.environ.get("ALGO_SELF_HEAL_LOG_FILE") or os.environ.get("ALGO_PAPER_LOG_FILE")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(paper_full_log_path(ROOT))
    return candidates


def collect_logs(runner: CommandRunner, env: str, since: str) -> str:
    log_file = os.environ.get("ALGO_SELF_HEAL_LOG_FILE")
    if log_file:
        text = _read_file(Path(log_file))
        print(f"SELF_HEAL_LOG_SOURCE source=file path={log_file}")
        return text
    if platform_name() == "Darwin" and env_upper(env) == "PAPER":
        paper_review_dir(ROOT).mkdir(parents=True, exist_ok=True)
        path = _latest_existing_file(_paper_file_log_candidates())
        if path is not None:
            print(f"SELF_HEAL_LOG_SOURCE source=file path={path}")
            return _read_file(path)
        expected = paper_full_log_path(ROOT)
        print(f"SELF_HEAL_LOG_SOURCE source=none reason=missing_review_log path={expected}")
        return f"PAPER_REVIEW_LOG_MISSING path={expected}\n"
    unit = service_name(env)
    try:
        result = runner.run(["journalctl", "-u", unit, "--since", since, "--no-pager"])
    except FileNotFoundError:
        print("SELF_HEAL_LOG_SOURCE source=none reason=journalctl_unavailable")
        return ""
    print("SELF_HEAL_LOG_SOURCE source=journalctl")
    return result.stdout + result.stderr


def latest_metrics_report(data_dir: Path, env: str) -> Path | None:
    root = data_dir / "research_metrics"
    if not root.exists():
        return None
    suffix = f"_{env_lower(env)}.json"
    candidates = [
        path
        for path in root.glob("*/*" + suffix)
        if path.is_file() and path.name.startswith(("begin_day_", "end_day_"))
    ]
    if not candidates:
        return None
    phase_rank = {"end_day": 1, "begin_day": 0}

    def sort_key(path: Path) -> tuple[str, int, float]:
        phase = "end_day" if path.name.startswith("end_day_") else "begin_day"
        return (path.parent.name, phase_rank[phase], path.stat().st_mtime)

    return sorted(candidates, key=sort_key)[-1]


def metrics_diagnostic_to_evidence(path: Path, env: str) -> FailureEvidence | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if str(payload.get("environment") or "").upper() != env_upper(env):
        return None
    diagnostics = (
        payload.get("logs", {}).get("missing_flow_diagnostics", [])
        if isinstance(payload.get("logs"), dict)
        else []
    )
    if not isinstance(diagnostics, list):
        return None
    for item in diagnostics:
        if not isinstance(item, dict):
            continue
        short_failure = str(item.get("short_failure") or "").strip()
        if env_upper(env) == "LIVE" and short_failure not in CRITICAL_MISSING_FLOW_FAILURES:
            continue
        expected = str(item.get("expected_flow") or "Expected live trading flow should complete the next handoff.")
        missing = str(item.get("actual_missing_step") or "missing downstream handoff")
        fingerprint = str(item.get("fingerprint") or _fingerprint(env, short_failure, missing))
        report_date = str(payload.get("date") or path.parent.name)
        phase = str(payload.get("phase") or path.stem)
        matching = (
            f"research_metrics_report={path}",
            f"date={report_date}",
            f"phase={phase}",
            f"diagnostic={json.dumps(item, sort_keys=True)}",
        )
        return FailureEvidence(
            short_failure=short_failure,
            expected_flow=expected,
            actual_missing_step=missing,
            grep_command=f"jq '.logs.missing_flow_diagnostics' {path}",
            matching_logs=matching,
            fingerprint=fingerprint,
            source_path=str(path),
        )
    return None


def evidence_from_metrics(data_dir: Path, env: str, explicit_report: Path | None = None) -> FailureEvidence | None:
    report = explicit_report or latest_metrics_report(data_dir, env)
    if report is None:
        return None
    return metrics_diagnostic_to_evidence(report, env)


def _selected_symbols(line: str) -> list[str]:
    match = re.search(r"selected=(\[[^\]]*\])", line)
    if not match:
        return []
    try:
        parsed = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return []
    return [str(item).upper() for item in parsed if str(item).strip()]


def _dynamic_candidate_symbol(line: str) -> str | None:
    match = re.search(r"symbol=([A-Z0-9._-]+)", line)
    if match:
        return match.group(1).upper()
    return None


def _line_epoch_seconds(line: str) -> float | None:
    patterns = (
        r"(?P<iso>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
        r"(?P<space>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})",
    )
    for pattern in patterns:
        match = re.search(pattern, line)
        if not match:
            continue
        value = match.group("iso") if "iso" in match.groupdict() and match.group("iso") else match.group("space")
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    match = re.search(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?\b", line)
    if not match:
        return None
    h = int(match.group("h"))
    m = int(match.group("m"))
    s = int(match.group("s") or 0)
    if h > 23 or m > 59 or s > 59:
        return None
    return float(h * 3600 + m * 60 + s)


def _line_timestamp_label(line: str) -> str:
    iso = re.search(r"\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+", line)
    if iso:
        return iso.group(0)
    match = re.search(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", line)
    if match:
        return match.group(0)
    return "unknown"


def _line_mentions_dynamic_success(line: str, symbol: str) -> bool:
    return symbol in line and any(marker in line for marker in DYNAMIC_ENTRY_RECOVERY_MARKERS)


def _line_mentions_business_skip(line: str, symbol: str) -> bool:
    if symbol not in line or "DYNAMIC_ENTRY_CANDIDATE_SKIPPED" not in line:
        return False
    lowered = line.lower()
    return any(reason in lowered for reason in DYNAMIC_ENTRY_BUSINESS_SKIP_REASONS)


def _line_mentions_dynamic_terminal(line: str, symbol: str) -> bool:
    return (
        _line_mentions_dynamic_success(line, symbol)
        or _line_mentions_business_skip(line, symbol)
        or (symbol in line and "DYNAMIC_ENTRY_EVAL_DROPPED" in line)
    )


def _reason_to_terminal_state(reason: str) -> str:
    clean = str(reason or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not clean:
        return "entry_eval_rejected"
    if "short_history" in clean or "not_enough_bars" in clean or "not_enough_bars" in clean:
        return "short_history"
    if "spread" in clean:
        return "spread_too_wide"
    if "unstable_quote" in clean or "bad_quote" in clean or "no_quote" in clean or "quote" in clean:
        return "unstable_quote"
    if "cooldown" in clean:
        return "cooldown"
    if "vwap" in clean:
        return "dynamic_vwap_extension"
    if "market_closed" in clean or "market_not_open" in clean:
        return "market_closed"
    if "account_state" in clean or "reduce_only" in clean or "buying_power" in clean or "exposure" in clean:
        return "account_state_changed"
    if "weak_catalyst" in clean or "no_catalyst" in clean:
        return "weak_catalyst_filter"
    if "allocator" in clean:
        return "allocator_rejected"
    if "dispatch" in clean or "dynamic_relative_volume" in clean or "dynamic_price_below_minimum" in clean:
        return "dispatch_rejected"
    if "entry_eval" in clean or "trend" in clean or "alignment" in clean or "rvol" in clean or "relative_volume" in clean:
        return "entry_eval_rejected"
    return "entry_eval_rejected"


def _extract_reason(line: str) -> str:
    match = re.search(r"\breason=([A-Za-z0-9_.:/<> -]+)", line)
    if match:
        return match.group(1).strip().split()[0]
    match = re.search(r"\bstage=([A-Za-z0-9_.:/<> -]+)", line)
    if match:
        return match.group(1).strip().split()[0]
    return "unknown"


def _terminal_state_from_line(line: str, symbol: str) -> tuple[str | None, str | None]:
    if symbol not in line:
        return None, None
    reason = _extract_reason(line)
    if "ORDER_SUBMITTED" in line or "ALLOCATOR_ACTION_SUBMITTED" in line:
        return "order_submitted", reason
    if "ALLOCATOR_DISPATCH_SKIPPED" in line or "ORDER_SKIP" in line:
        return "dispatch_rejected", reason
    if (
        "ALLOCATOR_REJECT" in line
        or "ALLOCATOR_FILTER_REJECT" in line
        or "ALLOCATOR_DROPPED" in line
        or "ALLOCATOR_SKIP" in line
    ):
        return "allocator_rejected", reason
    if "ENTRY_TERMINAL_OUTCOME" in line:
        if "stage=submitted" in line:
            return "order_submitted", reason
        if "stage=allocator_order_intent" in line or "stage=allocator_action_created" in line:
            return "entry_eval_completed", reason
        if "stage=allocator_filtered" in line or "stage=allocator_rejected" in line:
            return "allocator_rejected", reason
        if "stage=dispatch_rejected" in line:
            return "dispatch_rejected", reason
        return _reason_to_terminal_state(reason), reason
    if "ENTRY_EVAL_PASS" in line or "ENTRY_TO_ALLOCATOR_TRACE" in line:
        return "entry_eval_completed", reason
    if "DYNAMIC_ENTRY_EVAL_DROPPED" in line or "DYNAMIC_ENTRY_CANDIDATE_SKIPPED" in line:
        return _reason_to_terminal_state(reason), reason
    if "DYNAMIC_SELECTED_ENTRY_DROP" in line or "DYNAMIC_SELECTED_ENTRY_SKIPPED" in line:
        return _reason_to_terminal_state(reason), reason
    if "ENTRY_EVAL" in line and ("final=F" in line or "result=reject" in line or "rejected" in line.lower()):
        return "entry_eval_rejected", reason
    return None, None


def _state_rank(state: str | None) -> int:
    return TERMINAL_STATE_PRIORITY.get(str(state or ""), 0)


def _seconds_since_midnight(now: datetime) -> float:
    return float(now.hour * 3600 + now.minute * 60 + now.second)


def _dynamic_entry_flow_status(
    lines: Sequence[str],
    *,
    now: datetime | None = None,
) -> DynamicEntryFlowStatus:
    """Classify scanner-selected dynamic flow as observed, pending, failure, or none."""
    missing_symbols: list[str] = []
    pending_symbols: list[str] = []
    observed_symbols: set[str] = set()
    state_summaries: list[str] = []
    evidence: list[str] = []
    cycle_candidates: dict[str, dict[str, object]] = {}
    cycle_evidence: list[str] = []
    cycle_started_at: float | None = None
    latest_ts: float | None = None

    def elapsed_since_cycle() -> float | None:
        if cycle_started_at is None or latest_ts is None:
            if cycle_started_at is None or now is None:
                return None
            reference = now.timestamp() if cycle_started_at > 86400 else _seconds_since_midnight(now)
        else:
            reference = latest_ts
            if now is not None:
                now_reference = now.timestamp() if cycle_started_at > 86400 else _seconds_since_midnight(now)
                same_log_day = True
                if cycle_started_at > 86400:
                    same_log_day = datetime.fromtimestamp(cycle_started_at).date() == now.date()
                if same_log_day:
                    reference = max(reference, now_reference)
        delta = reference - cycle_started_at
        if delta < 0:
            return None
        return delta

    def close_cycle(*, final: bool = False) -> None:
        nonlocal cycle_candidates, cycle_evidence, cycle_started_at
        if not cycle_candidates:
            cycle_evidence = []
            cycle_started_at = None
            return
        for symbol in sorted(cycle_candidates):
            row = cycle_candidates[symbol]
            terminal_state = str(row.get("terminal_state") or "")
            terminal_reason = str(row.get("terminal_reason") or "unknown")
            if terminal_state in VALID_DYNAMIC_TERMINAL_STATES:
                observed_symbols.add(symbol)
                state_summaries.append(
                    "symbol=%s selected_at=%s entry_eval_started=%s entry_eval_completed=%s terminal_state=%s terminal_reason=%s"
                    % (
                        symbol,
                        row.get("selected_at") or "unknown",
                        str(bool(row.get("entry_eval_started"))).lower(),
                        str(bool(row.get("entry_eval_completed"))).lower(),
                        terminal_state,
                        terminal_reason,
                    )
                )
            elif final and (elapsed_since_cycle() is None or elapsed_since_cycle() < DYNAMIC_ENTRY_GRACE_SECONDS):
                pending_symbols.append(symbol)
            else:
                later_state = None
                later_reason = None
                for later_line in lines:
                    later_state_candidate, later_reason_candidate = _terminal_state_from_line(later_line, symbol)
                    if later_state_candidate and _state_rank(later_state_candidate) >= _state_rank(later_state):
                        later_state = later_state_candidate
                        later_reason = later_reason_candidate
                if later_state in VALID_DYNAMIC_TERMINAL_STATES:
                    observed_symbols.add(symbol)
                    state_summaries.append(
                        "symbol=%s selected_at=%s entry_eval_started=%s entry_eval_completed=%s terminal_state=%s terminal_reason=%s"
                        % (
                            symbol,
                            row.get("selected_at") or "unknown",
                            str(bool(row.get("entry_eval_started"))).lower(),
                            str(bool(row.get("entry_eval_completed"))).lower(),
                            later_state,
                            later_reason or "unknown",
                        )
                    )
                elif symbol not in missing_symbols:
                    missing_symbols.append(symbol)
                    state_summaries.append(
                        "symbol=%s selected_at=%s entry_eval_started=%s entry_eval_completed=%s terminal_state=missing_terminal_state terminal_reason=missing_terminal_state"
                        % (
                            symbol,
                            row.get("selected_at") or "unknown",
                            str(bool(row.get("entry_eval_started"))).lower(),
                            str(bool(row.get("entry_eval_completed"))).lower(),
                        )
                    )
        evidence.extend(cycle_evidence)
        cycle_candidates = {}
        cycle_evidence = []
        cycle_started_at = None

    for line in lines:
        ts = _line_epoch_seconds(line)
        if ts is not None:
            latest_ts = max(latest_ts, ts) if latest_ts is not None else ts
        if "DYNAMIC_SCAN selected=" in line:
            close_cycle(final=False)
            cycle_evidence = [line]
            cycle_candidates = {
                symbol: {
                    "selected_at": _line_timestamp_label(line),
                    "entry_eval_started": False,
                    "entry_eval_completed": False,
                    "terminal_state": None,
                    "terminal_reason": None,
                }
                for symbol in _selected_symbols(line)
            }
            cycle_started_at = ts
            continue
        if cycle_candidates:
            cycle_evidence.append(line)
            enqueued_symbol = (
                _dynamic_candidate_symbol(line)
                if "DYNAMIC_ENTRY_CANDIDATE_ENQUEUED" in line
                else None
            )
            if enqueued_symbol:
                cycle_candidates.setdefault(
                    enqueued_symbol,
                    {
                        "selected_at": _line_timestamp_label(line),
                        "entry_eval_started": False,
                        "entry_eval_completed": False,
                        "terminal_state": None,
                        "terminal_reason": None,
                    },
                )
            for symbol, row in cycle_candidates.items():
                if symbol not in line:
                    continue
                if "DYNAMIC_ENTRY_EVAL_START" in line or "DYNAMIC_SELECTED_ENTRY_EVAL_START" in line:
                    row["entry_eval_started"] = True
                state, reason = _terminal_state_from_line(line, symbol)
                if state:
                    if state == "entry_eval_completed":
                        row["entry_eval_completed"] = True
                    if _state_rank(state) >= _state_rank(str(row.get("terminal_state") or "")):
                        row["terminal_state"] = state
                        row["terminal_reason"] = reason or state
    close_cycle(final=True)
    if missing_symbols:
        return DynamicEntryFlowStatus(
            "failure",
            tuple(sorted(set(missing_symbols))),
            tuple(evidence + state_summaries),
            tuple(state_summaries),
        )
    if pending_symbols:
        return DynamicEntryFlowStatus("pending", tuple(sorted(set(pending_symbols))), tuple(evidence), tuple(state_summaries))
    if observed_symbols:
        return DynamicEntryFlowStatus("observed", tuple(sorted(observed_symbols)), tuple(evidence + state_summaries), tuple(state_summaries))
    return DynamicEntryFlowStatus("none")


def _fingerprint(env: str, failure: str, missing: str) -> str:
    raw = f"self-heal:{env_lower(env)}:{failure}:{missing}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", failure.lower()).strip("-")
    return f"self-heal:{env_lower(env)}:{slug}:{digest}"


def detect_failure(
    log_text: str,
    env: str,
    since: str = "30 minutes ago",
    *,
    now: datetime | None = None,
) -> FailureEvidence | None:
    lines = [redact_secrets(line) for line in log_text.splitlines()]
    traceback_lines = [line for line in lines if re.search(r"Traceback|UnboundLocalError|CRITICAL|FATAL", line, re.I)]
    if traceback_lines:
        failure = "service exception or traceback"
        missing = "runtime completed without unhandled exception"
        return FailureEvidence(
            short_failure=failure,
            expected_flow="Live loop should complete each cycle without unhandled exceptions.",
            actual_missing_step=missing,
            grep_command=f"journalctl -u {service_name(env)} --since '{since}' | grep -E 'Traceback|UnboundLocalError|CRITICAL|FATAL'",
            matching_logs=tuple(traceback_lines[-80:]),
            fingerprint=_fingerprint(env, failure, missing),
        )

    dynamic_flow = _dynamic_entry_flow_status(lines, now=now)
    if dynamic_flow.status == "failure":
        failure = "missing_terminal_state"
        missing = "scanner-selected symbols missing terminal state: " + ",".join(dynamic_flow.symbols[:8])
        evidence = [
            line
            for line in dynamic_flow.evidence
            if "DYNAMIC_SCAN selected=" in line
            or "DYNAMIC_ENTRY_CANDIDATE_ENQUEUED" in line
            or "DYNAMIC_PIPELINE_TERMINAL" in line
            or "missing_terminal_state" in line
            or any(symbol in line for symbol in dynamic_flow.symbols)
        ]
        return FailureEvidence(
            short_failure=failure,
            expected_flow=(
                "Every DYNAMIC_SCAN selected symbol should finish in exactly one explicit terminal state "
                "such as entry_eval_completed, entry_eval_rejected, allocator_rejected, dispatch_rejected, "
                "order_submitted, or an expected business skip."
            ),
            actual_missing_step=missing,
            grep_command=(
                f"journalctl -u {service_name(env)} --since '{since}' | "
                "grep -E 'DYNAMIC_SCAN selected|DYNAMIC_ENTRY_CANDIDATE_ENQUEUED|"
                "DYNAMIC_ENTRY_EVAL_START|DYNAMIC_ENTRY_EVAL_DROPPED|ENTRY_EVAL|"
                "ENTRY_TO_ALLOCATOR_TRACE|ALLOCATOR ACTIONS|ORDER_SUBMITTED|DYNAMIC_ENTRY_CANDIDATE_SKIPPED|"
                "ENTRY_TERMINAL_OUTCOME|DYNAMIC_PIPELINE_TERMINAL'"
            ),
            matching_logs=tuple(evidence[-120:]),
            fingerprint=_fingerprint(env, failure, "dynamic-terminal-state"),
        )

    checks = (
        (
            "entry eval pass without allocator trace",
            "ENTRY_EVAL_PASS",
            ("ENTRY_TO_ALLOCATOR_TRACE",),
            "ENTRY_TO_ALLOCATOR_TRACE",
        ),
        (
            "allocator dispatch without action or reject reasons",
            "ENTRY_TO_ALLOCATOR",
            ("ALLOCATOR ACTIONS", "ALLOCATOR_ACTION", "reject_reasons", "reject reason", "ALLOCATOR_REJECT"),
            "ALLOCATOR ACTIONS or reject_reasons",
        ),
        (
            "submitted order without status confirmation",
            "ORDER_SUBMITTED",
            ("ORDER_FILLED", "ORDER_STATUS", "POSITION_CONFIRM", "fill", "status", "position confirmation"),
            "fill/status/position confirmation",
        ),
    )
    for failure, prior, expected_markers, missing in checks:
        if any(prior in line for line in lines) and not any(marker in line for line in lines for marker in expected_markers):
            evidence = [line for line in lines if prior in line or any(marker in line for marker in expected_markers)]
            return FailureEvidence(
                short_failure=failure,
                expected_flow=f"{prior} should be followed by {missing}.",
                actual_missing_step=missing,
                grep_command=f"journalctl -u {service_name(env)} --since '{since}' | grep -E '{prior}|{missing}'",
                matching_logs=tuple(evidence[-120:]),
                fingerprint=_fingerprint(env, failure, missing),
            )
    return None


def build_issue_body(env: str, evidence: FailureEvidence, since: str) -> str:
    logs = "\n".join(evidence.matching_logs) or "no matching logs captured"
    metrics_line = f"- Research Metrics Report: {evidence.source_path}\n\n" if evidence.source_path else ""
    return (
        f"Self-heal detected a live trading-flow/runtime regression.\n\n"
        f"- Environment: {env_upper(env)}\n"
        f"- environment={env_lower(env)}\n"
        f"- Time window: {since}\n"
        f"- Fingerprint: {evidence.fingerprint}\n\n"
        f"{metrics_line}"
        "## Evidence\n\n"
        f"- grep command: `{evidence.grep_command}`\n"
        f"- expected flow: {evidence.expected_flow}\n"
        f"- actual missing step: {evidence.actual_missing_step}\n\n"
        "```text\n"
        f"{logs}\n"
        "```\n\n"
        "## Codex Scope\n\n"
        "- Prioritize wiring/runtime regressions and missing flow handoffs.\n"
        "- Do not auto-fix strategy thresholds unless this issue explicitly asks for threshold changes.\n"
        "- Preserve quote, spread, ATR/range, price, exposure, max-position, stop-loss, and order safety gates.\n"
    )


def ensure_github_labels(runner: CommandRunner, *, dry_run: bool) -> None:
    if dry_run:
        print("[dry-run] Would bootstrap required GitHub labels")
        return
    result = runner.run(["gh", "label", "list", "--json", "name", "--limit", "300"])
    existing: set[str] = set()
    if result.returncode == 0:
        try:
            payload = json.loads(result.stdout or "[]")
            existing = {str(item.get("name")) for item in payload if isinstance(item, dict)}
        except json.JSONDecodeError:
            existing = set()
    for label, (color, description) in REQUIRED_GITHUB_LABELS.items():
        if label in existing:
            continue
        create = runner.run(
            [
                "gh",
                "label",
                "create",
                label,
                "--color",
                color,
                "--description",
                description,
            ]
        )
        if create.returncode == 0:
            print(f"SELF_HEAL label=created name={label}")
            continue
        # A concurrent bootstrap may have created it; do not fail issue creation on that race.
        if "already exists" not in (create.stdout + create.stderr).lower():
            print(f"SELF_HEAL label=bootstrap_warning name={label} rc={create.returncode}")


def find_duplicate_issue(runner: CommandRunner, evidence: FailureEvidence) -> int | None:
    result = runner.run(
        ["gh", "issue", "list", "--state", "open", "--search", evidence.fingerprint, "--json", "number,title,body", "--limit", "20"]
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for issue in payload:
        text = f"{issue.get('title', '')}\n{issue.get('body', '')}"
        if evidence.fingerprint in text:
            return int(issue["number"])
    return None


def create_or_update_issue(
    runner: CommandRunner,
    env: str,
    evidence: FailureEvidence,
    since: str,
    *,
    dry_run: bool,
) -> int | None:
    title = f"[{env_upper(env)}] Self-heal: {evidence.short_failure}"
    body = build_issue_body(env, evidence, since)
    duplicate = None if dry_run else find_duplicate_issue(runner, evidence)
    if duplicate is not None:
        if dry_run:
            print(f"[dry-run] Would update duplicate issue #{duplicate}: {title}")
        else:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
                handle.write(body)
                body_path = handle.name
            runner.run(["gh", "issue", "comment", str(duplicate), "--body-file", body_path], check=True)
            Path(body_path).unlink(missing_ok=True)
            print(f"SELF_HEAL issue=duplicate number={duplicate}")
        return duplicate
    if dry_run:
        print(f"[dry-run] Would create GitHub issue: {title}")
        print(f"[dry-run] Fingerprint: {evidence.fingerprint}")
        return None
    ensure_github_labels(runner, dry_run=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    result = runner.run(
        [
            "gh",
            "issue",
            "create",
            "--title",
            title,
            "--body-file",
            body_path,
            "--label",
            "codex",
            "--label",
            "auto-fix",
            "--label",
            "algo-health",
            "--label",
            env_upper(env),
            "--label",
            f"environment:{env_lower(env)}",
            "--label",
            processor_label(env),
        ],
        check=True,
    )
    Path(body_path).unlink(missing_ok=True)
    number_match = re.search(r"/issues/(\d+)", result.stdout)
    issue_number = int(number_match.group(1)) if number_match else None
    print(f"SELF_HEAL issue=created number={issue_number or 'unknown'}")
    return issue_number


def _parse_codex_result(output: str, issue_number: int | None) -> CodexRouteResult | None:
    pattern = re.compile(r"CODEX_RESULT\s+issue=(?P<issue>\d+|unknown)\s+status=(?P<status>[a-z_]+)(?:\s+reason=(?P<reason>[a-zA-Z0-9_.:-]+))?")
    matches = list(pattern.finditer(output))
    if not matches:
        return None
    match = matches[-1]
    issue_text = match.group("issue")
    parsed_issue = int(issue_text) if issue_text.isdigit() else issue_number
    return CodexRouteResult(
        status=match.group("status"),
        reason=match.group("reason"),
        issue_number=parsed_issue,
    )


def route_to_codex(runner: CommandRunner, issue_number: int | None, *, dry_run: bool) -> CodexRouteResult:
    command = ["scripts/process_codex_issues_local.sh", "--limit", "1"]
    if issue_number is not None:
        command.extend(["--issue", str(issue_number)])
    if dry_run:
        print("[dry-run] Would route issue to Codex: " + " ".join(command))
        return CodexRouteResult(status="dry_run", issue_number=issue_number)
    result = runner.run(command, check=False)
    combined = result.stdout + result.stderr
    parsed = _parse_codex_result(combined, issue_number)
    if parsed is not None:
        if parsed.status == "failed" and parsed.reason == "dirty_worktree":
            print(f"SELF_HEAL status=blocked reason=dirty_worktree issue={issue_number or parsed.issue_number or 'unknown'}")
        elif parsed.status == "codex_running":
            print(f"SELF_HEAL status=codex_running issue={issue_number or parsed.issue_number or 'unknown'}")
        elif parsed.status == "fix_local_only":
            print(
                "SELF_HEAL status=blocked reason=codex_fix_local_only "
                f"issue={issue_number or parsed.issue_number or 'unknown'} next_action=push_branch_and_open_pr"
            )
        elif parsed.status == "failed":
            print(
                f"SELF_HEAL status=blocked reason={parsed.reason or 'codex_failed'} "
                f"issue={issue_number or parsed.issue_number or 'unknown'}"
            )
        return parsed
    if result.returncode != 0:
        if "Worktree is dirty" in combined:
            print(f"SELF_HEAL status=blocked reason=dirty_worktree issue={issue_number or 'unknown'}")
            return CodexRouteResult(status="failed", reason="dirty_worktree", issue_number=issue_number)
        if "lock already exists" in combined:
            print(f"SELF_HEAL status=codex_running issue={issue_number or 'unknown'}")
            return CodexRouteResult(status="codex_running", issue_number=issue_number)
        print(f"SELF_HEAL status=blocked reason=codex_processor_failed issue={issue_number or 'unknown'}")
        return CodexRouteResult(status="failed", reason="codex_processor_failed", issue_number=issue_number)
    return CodexRouteResult(status="unknown", issue_number=issue_number)


def find_pr_for_issue(runner: CommandRunner, issue_number: int | None) -> int | None:
    if issue_number is None:
        return None
    result = runner.run(
        ["gh", "pr", "list", "--state", "open", "--search", f"#{issue_number}", "--json", "number,title,body", "--limit", "20"]
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    for pr in payload:
        text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
        if f"#{issue_number}" in text:
            return int(pr["number"])
    return None


def validate_and_merge_pr(runner: CommandRunner, issue_number: int | None, *, dry_run: bool) -> int | None:
    if dry_run:
        print("[dry-run] Would validate Codex PR and auto-merge only after checks pass")
        return None
    pr_number = find_pr_for_issue(runner, issue_number)
    if pr_number is None:
        print("SELF_HEAL pr=none")
        return None
    runner.run(["scripts/validate_codex_pr.sh"], check=True)
    runner.run(["gh", "pr", "checks", str(pr_number), "--watch", "--fail-fast"], check=True)
    runner.run(["gh", "pr", "merge", str(pr_number), "--squash", "--delete-branch"], check=True)
    print(f"SELF_HEAL pr=merged number={pr_number}")
    return pr_number


def in_market_open_guard(now: datetime | None = None) -> bool:
    current = now or datetime.now(ZoneInfo("America/New_York"))
    if current.weekday() >= 5:
        return False
    return time(9, 30) <= current.time() < time(9, 45)


def deploy_guard_failure(runner: CommandRunner, env: str, *, manual_override: bool) -> str | None:
    if env_upper(env) == "LIVE" and in_market_open_guard() and not manual_override:
        return "first_15_minutes_market_open"
    status = runner.run(["git", "status", "--short"])
    if status.stdout.strip():
        return "repo_dirty"
    service = service_name(env)
    state = runner.run(["systemctl", "is-active", service])
    active_state = (state.stdout + state.stderr).strip()
    if active_state in {"activating", "deactivating", "reloading"}:
        return "service_already_restarting"
    return None


def create_followup_issue(
    runner: CommandRunner,
    env: str,
    issue_number: int | None,
    pr_number: int | None,
    reason: str,
    logs: str,
    *,
    dry_run: bool,
) -> None:
    title = f"[{env_upper(env)}] Self-heal: post-deploy verification failed"
    body = (
        "Self-heal deployment verification failed.\n\n"
        f"- Environment: {env_upper(env)}\n"
        f"- Original Issue: #{issue_number or 'unknown'}\n"
        f"- Repair PR: #{pr_number or 'unknown'}\n"
        f"- Reason: {reason}\n\n"
        "```text\n"
        f"{redact_secrets(logs)[-6000:]}\n"
        "```\n"
    )
    if dry_run:
        print(f"[dry-run] Would create follow-up GitHub issue: {title}")
        return
    if issue_number is not None:
        body = (
            "Self-heal post-deploy verification is still failing; keeping this issue open.\n\n"
            f"- Environment: {env_upper(env)}\n"
            f"- Repair PR: #{pr_number or 'unknown'}\n"
            f"- Reason: {reason}\n\n"
            "```text\n"
            f"{redact_secrets(logs)[-6000:]}\n"
            "```\n"
        )
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
            handle.write(body)
            body_path = handle.name
        runner.run(["gh", "issue", "comment", str(issue_number), "--body-file", body_path], check=True)
        Path(body_path).unlink(missing_ok=True)
        print(f"SELF_HEAL status=verification_failed issue={issue_number} reason={reason}")
        return
    ensure_github_labels(runner, dry_run=False)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    runner.run(
        [
            "gh",
            "issue",
            "create",
            "--title",
            title,
            "--body-file",
            body_path,
            "--label",
            "codex",
            "--label",
            "auto-fix",
            "--label",
            "algo-health",
            "--label",
            env_upper(env),
            "--label",
            f"environment:{env_lower(env)}",
            "--label",
            processor_label(env),
        ],
        check=True,
    )
    Path(body_path).unlink(missing_ok=True)


def _traceback_signature_from_evidence(evidence: FailureEvidence | None) -> str | None:
    if evidence is None:
        return None
    for line in evidence.matching_logs:
        if "UnboundLocalError" in line:
            return "UnboundLocalError"
        if "Traceback" in line:
            return "Traceback"
    return None


def close_issue_recovered(
    runner: CommandRunner,
    env: str,
    issue_number: int | None,
    pr_number: int | None,
    *,
    dry_run: bool,
) -> None:
    if issue_number is None:
        return
    body = (
        "Self-heal post-deploy verification passed.\n\n"
        f"- Environment: {env_upper(env)}\n"
        f"- Repair PR: #{pr_number or 'unknown'}\n"
        "- systemctl active: yes\n"
        "- Traceback check window: clean\n"
    )
    if dry_run:
        print(f"[dry-run] Would close recovered issue #{issue_number}")
        return
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".md", delete=False) as handle:
        handle.write(body)
        body_path = handle.name
    runner.run(["gh", "issue", "comment", str(issue_number), "--body-file", body_path], check=True)
    Path(body_path).unlink(missing_ok=True)
    runner.run(["gh", "issue", "close", str(issue_number), "--reason", "completed"], check=True)
    print(f"SELF_HEAL status=recovered issue={issue_number}")


def deploy_and_verify(
    runner: CommandRunner,
    env: str,
    issue_number: int | None,
    pr_number: int | None,
    *,
    dry_run: bool,
    manual_override: bool,
    evidence: FailureEvidence | None = None,
) -> bool:
    guard = None if dry_run else deploy_guard_failure(runner, env, manual_override=manual_override)
    if guard:
        print(f"SELF_HEAL deploy=skipped reason={guard}")
        return False
    service = service_name(env)
    if dry_run:
        print("[dry-run] Would git checkout main && git pull --ff-only")
        print(f"[dry-run] Would systemctl restart {service}")
        print(f"[dry-run] Would verify systemctl active and no Traceback in last 2 min")
        return True
    runner.run(["git", "checkout", "main"], check=True)
    runner.run(["git", "pull", "--ff-only"], check=True)
    runner.run(["systemctl", "restart", service], check=True)
    active = runner.run(["systemctl", "is-active", service])
    logs = runner.run(["journalctl", "-u", service, "--since", "2 minutes ago", "--no-pager"])
    combined_logs = logs.stdout + logs.stderr
    signature = _traceback_signature_from_evidence(evidence)
    if active.stdout.strip() != "active":
        create_followup_issue(runner, env, issue_number, pr_number, "service_not_active", active.stdout + active.stderr, dry_run=False)
        return False
    if signature and signature in combined_logs:
        create_followup_issue(runner, env, issue_number, pr_number, f"original_signature_after_restart:{signature}", combined_logs, dry_run=False)
        return False
    if re.search(r"Traceback|UnboundLocalError|CRITICAL|FATAL", combined_logs, re.I):
        create_followup_issue(runner, env, issue_number, pr_number, "traceback_after_restart", combined_logs, dry_run=False)
        return False
    print(f"SELF_HEAL deploy=verified service={service}")
    close_issue_recovered(runner, env, issue_number, pr_number, dry_run=False)
    return True


def run_self_heal(args: argparse.Namespace, runner: CommandRunner) -> int:
    env = "LIVE" if args.live else "PAPER"
    metrics_report = Path(args.metrics_report) if args.metrics_report else None
    evidence = evidence_from_metrics(Path(args.data_dir), env, metrics_report)
    source = "research_metrics" if evidence is not None else "logs"
    if evidence is None:
        logs = collect_logs(runner, env, args.since)
        if env == "PAPER" and "PAPER_REVIEW_LOG_MISSING" in logs:
            path = logs.strip().split("path=", 1)[-1] if "path=" in logs else "unknown"
            print(f"SELF_HEAL status=degraded env=paper reason=missing_review_log path={path}")
            return 0
        now = datetime.now(ZoneInfo("America/New_York"))
        dynamic_status = _dynamic_entry_flow_status(
            [redact_secrets(line) for line in logs.splitlines()],
            now=now,
        )
        if dynamic_status.status == "observed":
            print("SELF_HEAL status=healthy reason=dynamic_entry_eval_observed")
            return 0
        if dynamic_status.status == "pending":
            print("SELF_HEAL status=blocked reason=entry_eval_pending")
            return 0
        evidence = detect_failure(logs, env, args.since, now=now)
    if evidence is None:
        print(f"SELF_HEAL status=healthy env={env_lower(env)}")
        return 0
    print(f"SELF_HEAL status=failure_detected env={env_lower(env)} source={source} failure={evidence.short_failure}")
    issue_number = create_or_update_issue(runner, env, evidence, args.since, dry_run=args.dry_run)
    codex_result = route_to_codex(runner, issue_number, dry_run=args.dry_run)
    if codex_result.status in {"failed", "fix_local_only", "codex_running", "no_change"}:
        return 0
    pr_number = validate_and_merge_pr(runner, issue_number, dry_run=args.dry_run)
    if pr_number is None and not args.dry_run:
        print(f"SELF_HEAL status=blocked reason=no_pr issue={issue_number or 'unknown'}")
        return 0
    if deploy_and_verify(
        runner,
        env,
        issue_number,
        pr_number,
        dry_run=args.dry_run,
        manual_override=args.manual_override,
        evidence=evidence,
    ):
        return 0
    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create/update and route self-heal issues from research metrics or logs.")
    env_group = parser.add_mutually_exclusive_group(required=True)
    env_group.add_argument("--live", action="store_true", help="run against live algo.service")
    env_group.add_argument("--paper", action="store_true", help="run against paper.service")
    parser.add_argument("--dry-run", action="store_true", help="show issue/Codex actions without creating issues or routing Codex")
    parser.add_argument("--manual-override", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--since", default=os.environ.get("ALGO_SELF_HEAL_SINCE", "30 minutes ago"))
    parser.add_argument("--data-dir", default=os.environ.get("ALGO_SELF_HEAL_DATA_DIR", str(ROOT / "data")))
    parser.add_argument("--metrics-report", default=os.environ.get("ALGO_SELF_HEAL_METRICS_REPORT"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    return run_self_heal(parse_args(argv or sys.argv[1:]), CommandRunner())


if __name__ == "__main__":
    raise SystemExit(main())
