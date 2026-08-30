from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import analyze_algo_logs as aal


def write_analysis_config(
    root: Path,
    *,
    github_issue_enabled: bool = True,
    github_comment_enabled: bool = False,
    auto_close_resolved_issues: bool = False,
) -> None:
    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_dir.joinpath("default.yaml").write_text(
        "\n".join(
            [
                "analysis:",
                "  enabled: true",
                f"  github_issue_enabled: {str(github_issue_enabled).lower()}",
                "  interval_minutes: 5",
                "  lookback_minutes: 30",
                "  duplicate_window_hours: 24",
                "  processor_label_enabled: false",
                "  processor_labels:",
                '    paper: "processor:mac-paper"',
                '    live: "processor:live"',
                f"  github_comment_enabled: {str(github_comment_enabled).lower()}",
                f"  auto_close_resolved_issues: {str(auto_close_resolved_issues).lower()}",
                "  issue_thresholds:",
                "    skip_reason: 5",
                "    traceback: 1",
                "    exception: 1",
                "    service_restart: 2",
                "    entry_eval_to_skip: 10",
                "    allocator_dispatch_skip: 3",
                "    unstable_quote: 150",
                "    spread_too_wide: 15",
                "    expected_strategy_skip: 50",
                "    pipeline_skip_after_entry: 1",
                "",
            ]
        ),
        encoding="utf-8",
    )


def analyzer_issue_body(
    *,
    environment: str = "live",
    reason: str = "dynamic_price_below_minimum",
    fingerprint: str = "log-analysis:live:execution:dynamic_price_below_minimum",
    snippet: str = "ORDER_SKIP symbol=RPAY reason=dynamic_price_below_minimum",
) -> str:
    return (
        "Automated log analyzer detected a recurring AlgoSphere runtime problem.\n\n"
        f"- Environment: {environment.upper()}\n"
        f"- environment={environment}\n"
        f"- Reason: {reason}\n"
        f"- Fingerprint: {fingerprint}\n\n"
        "## Evidence\n\n"
        "```text\n"
        f"{snippet}\n"
        "```\n"
    )


class FakeRunner(aal.CommandRunner):
    def __init__(
        self,
        issue_list: str = "[]",
        labels: list[str] | None = None,
        issue_body: str | None = None,
        issue_labels: list[str] | None = None,
    ) -> None:
        self.issue_list = issue_list
        self.labels = labels
        self.issue_body = issue_body
        self.issue_labels = issue_labels or ["codex", "auto-fix", "algo-failure", "environment:live", "severity:medium"]
        self.calls: list[list[str]] = []
        self.comment_bodies: list[str] = []

    def run(self, args, *, check: bool = False):
        argv = list(args)
        self.calls.append(argv)
        if argv[:3] == ["gh", "label", "list"]:
            if self.labels is None:
                return subprocess.CompletedProcess(argv, 1, "", "label list failed")
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps([{"name": label} for label in self.labels]),
                "",
            )
        if argv[:4] == ["gh", "issue", "list", "--state"]:
            return subprocess.CompletedProcess(argv, 0, self.issue_list, "")
        if argv[:3] == ["gh", "issue", "view"]:
            body = self.issue_body or ""
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "number": int(argv[3]),
                        "title": "issue",
                        "body": body,
                        "labels": [{"name": label} for label in self.issue_labels],
                    }
                ),
                "",
            )
        if argv[:3] == ["gh", "issue", "create"]:
            return subprocess.CompletedProcess(argv, 0, "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/321\n", "")
        if argv[:3] == ["gh", "issue", "comment"]:
            if "--body-file" in argv:
                self.comment_bodies.append(Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8"))
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:3] == ["gh", "issue", "close"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv and argv[0] == "journalctl":
            return subprocess.CompletedProcess(argv, 0, "ORDER_SKIP symbol=INTC reason=dynamic_relative_volume\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_collect_logs_uses_journalctl_when_available(monkeypatch) -> None:
    runner = FakeRunner()
    monkeypatch.setattr(aal.shutil, "which", lambda name: "/usr/bin/journalctl" if name == "journalctl" else None)

    logs = aal.collect_logs(environment="live", lookback_minutes=15, runner=runner)

    assert "ORDER_SKIP symbol=INTC reason=dynamic_relative_volume" in logs
    assert ["journalctl", "-u", "algo.service", "--since", "15 min ago", "--no-pager"] in runner.calls


def test_collect_logs_macos_without_journalctl_uses_configured_paper_log(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    log_path = tmp_path / "paper_full.log"
    log_path.write_text("ENTRY_EVAL_PASS symbol=INTC\n", encoding="utf-8")
    runner = FakeRunner()
    monkeypatch.setenv("ALGO_PAPER_LOG_FILE", str(log_path))
    monkeypatch.delenv("ALGO_ANALYZE_LOG_FILE", raising=False)
    monkeypatch.setattr(aal.shutil, "which", lambda name: None)

    logs = aal.collect_logs(environment="paper", lookback_minutes=15, root=tmp_path, runner=runner)

    assert logs == "ENTRY_EVAL_PASS symbol=INTC\n"
    assert runner.calls == []
    assert f"LOG_ANALYZER_LOG_SOURCE source=file path={log_path}" in capsys.readouterr().err


def test_collect_logs_macos_without_journalctl_missing_log_is_graceful(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    runner = FakeRunner()
    monkeypatch.delenv("ALGO_PAPER_LOG_FILE", raising=False)
    monkeypatch.delenv("ALGO_ANALYZE_LOG_FILE", raising=False)
    monkeypatch.setattr(aal.shutil, "which", lambda name: None)

    logs = aal.collect_logs(environment="paper", lookback_minutes=15, root=tmp_path, runner=runner)

    assert logs == ""
    assert runner.calls == []
    err = capsys.readouterr().err
    assert "LOG_ANALYZER_LOG_SOURCE source=none reason=journalctl_unavailable" in err
    assert "set ALGO_PAPER_LOG_FILE or pass --log-file" in err


def test_repeated_skip_aggregation_detects_threshold() -> None:
    logs = "\n".join(
        f"2026-06-29T10:0{i}:00 ORDER_SKIP symbol=RPAY reason=dynamic_price_below_minimum"
        for i in range(5)
    )

    findings = aal.analyze_log_text(logs, environment="live")

    assert len(findings) == 1
    assert findings[0].reason == "dynamic_price_below_minimum"
    assert findings[0].count == 5
    assert findings[0].severity == "medium"
    assert findings[0].classification == "pipeline_inconsistency"


def test_repeated_dynamic_relative_volume_without_entry_pass_is_not_issue_162_noise() -> None:
    logs = "\n".join(
        f"2026-06-29T10:{i:02d}:00 ORDER_SKIP symbol=INTC reason=dynamic_relative_volume source=capital_allocator"
        for i in range(38)
    )

    findings = aal.analyze_log_text(logs, environment="paper")

    assert findings == []


def test_issue_threshold_logic_ignores_single_skip() -> None:
    logs = "2026-06-29T10:01:00 ORDER_SKIP symbol=RPAY reason=dynamic_price_below_minimum"

    findings = aal.analyze_log_text(logs, environment="live")

    assert findings == []


def test_traceback_detection_is_immediate() -> None:
    logs = "\n".join(
        [
            "2026-06-29T10:01:00 INFO loop",
            "2026-06-29T10:01:01 Traceback (most recent call last):",
            "2026-06-29T10:01:02 ModuleNotFoundError: No module named 'alpaca'",
        ]
    )

    findings = aal.analyze_log_text(logs, environment="live")

    reasons = {finding.reason for finding in findings}
    assert "traceback" in reasons
    assert "module_not_found" in reasons
    assert all(finding.severity == "high" for finding in findings)


def test_name_error_traceback_is_classified_as_hard_error() -> None:
    logs = "\n".join(
        [
            "Jun 30 15:24:23 algosphere-live-host python3.12[1643026]: Traceback (most recent call last):",
            "Jun 30 15:24:23 algosphere-live-host python3.12[1643026]: 15:24 ET [live_bot] ERROR: NameError: name 'pos' is not defined - skipping to next user",
        ]
    )

    findings = aal.analyze_log_text(logs, environment="live")

    hard_errors = [finding for finding in findings if finding.classification == "hard_error"]
    assert hard_errors
    assert {finding.reason for finding in hard_errors} == {"traceback", "exception"}
    assert all(finding.severity == "high" for finding in hard_errors)
    assert any("NameError: name 'pos' is not defined" in "\n".join(finding.snippets) for finding in hard_errors)


def test_fingerprint_generation_is_stable_and_trace_sensitive() -> None:
    one = aal.stable_fingerprint(
        environment="live",
        component="execution",
        reason="dynamic_price_below_minimum",
    )
    two = aal.stable_fingerprint(
        environment="live",
        component="execution",
        reason="dynamic_price_below_minimum",
    )
    traced = aal.stable_fingerprint(
        environment="live",
        component="execution",
        reason="dynamic_price_below_minimum",
        stack_trace="Traceback\nValueError: bad",
    )

    assert one == two
    assert one == "log-analysis:live:execution:dynamic_price_below_minimum"
    assert traced.startswith(one + ":")
    assert traced != one


def test_duplicate_suppression_uses_local_fingerprint_state(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:live:market_data:unstable_quote",
        title="[LIVE] Repeated unstable quote blocks",
        component="market_data",
        reason="unstable_quote",
        severity="low",
        count=5,
        first_seen="2026-06-29T10:00:00",
        last_seen="2026-06-29T10:05:00",
        classification="market_data_noise",
    )
    state_path = tmp_path / "fingerprints.json"
    state_path.write_text(
        json.dumps(
            {
                "fingerprints": {
                    finding.fingerprint: {
                        "last_seen": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                        "seen_count": 1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="live",
        lookback_minutes=15,
        duplicate_window_hours=24,
        state_path=state_path,
        runner=FakeRunner(),
        dry_run=False,
    )

    assert (created, suppressed, duplicates) == (0, 1, 0)


def test_github_payload_generation_and_labels(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:paper:runtime:traceback:abc123",
        title="[PAPER] Runtime traceback in algo logs",
        component="runtime",
        reason="traceback",
        severity="high",
        count=1,
        first_seen="2026-06-29T10:00:00",
        last_seen="2026-06-29T10:00:00",
        classification="hard_error",
        snippets=["Traceback (most recent call last):", "RuntimeError: boom"],
    )
    state_path = tmp_path / "fingerprints.json"
    runner = FakeRunner()

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="paper",
        lookback_minutes=15,
        duplicate_window_hours=24,
        state_path=state_path,
        runner=runner,
        dry_run=False,
    )

    assert (created, suppressed, duplicates) == (1, 0, 0)
    create_calls = [call for call in runner.calls if call[:3] == ["gh", "issue", "create"]]
    assert len(create_calls) == 1
    create = create_calls[0]
    assert "--label" in create
    assert "codex" in create
    assert "auto-fix" in create
    assert "algo-failure" in create
    assert "environment:paper" in create
    assert "severity:medium" in create
    assert "processor:mac-paper" in create
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["fingerprints"][finding.fingerprint]["action"] == "created"


def test_paper_pipeline_inconsistency_creates_issue_despite_local_duplicate_state(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:paper:entry_to_execution:dynamic_relative_volume_after_entry_pass",
        title="[PAPER] dynamic_relative_volume after ENTRY_EVAL_PASS",
        component="entry_to_execution",
        reason="dynamic_relative_volume_after_entry_pass",
        severity="medium",
        count=2,
        first_seen="line:1",
        last_seen="line:2",
        classification="pipeline_inconsistency",
    )
    state_path = tmp_path / "fingerprints.json"
    state_path.write_text(
        json.dumps(
            {
                "fingerprints": {
                    finding.fingerprint: {
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                        "seen_count": 3,
                        "action": "local_duplicate_suppressed",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    runner = FakeRunner()

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="paper",
        lookback_minutes=15,
        duplicate_window_hours=24,
        state_path=state_path,
        runner=runner,
        dry_run=False,
    )

    assert (created, suppressed, duplicates) == (1, 0, 0)
    create = [call for call in runner.calls if call[:3] == ["gh", "issue", "create"]][0]
    assert "environment:paper" in create
    assert "processor:mac-paper" in create
    assert "severity:medium" in create


def test_paper_pipeline_inconsistency_count_fifty_gets_high_severity_label(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:paper:entry_to_execution:entry_eval_pass_to_dispatch_skip",
        title="[PAPER] ENTRY_EVAL_PASS followed by dispatch/order skips",
        component="entry_to_execution",
        reason="entry_eval_pass_to_dispatch_skip",
        severity="medium",
        count=50,
        first_seen="line:1",
        last_seen="line:50",
        classification="pipeline_inconsistency",
    )
    runner = FakeRunner()

    aal.process_findings(
        [finding],
        environment="paper",
        lookback_minutes=15,
        duplicate_window_hours=24,
        state_path=tmp_path / "fingerprints.json",
        runner=runner,
        dry_run=False,
    )

    create = [call for call in runner.calls if call[:3] == ["gh", "issue", "create"]][0]
    assert "severity:high" in create


def test_open_github_duplicate_is_ignored(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:live:allocator:allocator_action_to_dispatch_skip",
        title="[LIVE] Allocator actions followed by dispatch skips",
        component="allocator",
        reason="allocator_action_to_dispatch_skip",
        severity="medium",
        count=3,
        first_seen="line:1",
        last_seen="line:3",
        classification="pipeline_inconsistency",
    )
    runner = FakeRunner(
        json.dumps(
            [
                {
                    "number": 99,
                    "title": "existing",
                    "body": f"environment=live\nFingerprint: {finding.fingerprint}",
                    "labels": [{"name": "algo-failure"}, {"name": "environment:live"}],
                }
            ]
        )
    )

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="live",
        lookback_minutes=15,
        duplicate_window_hours=24,
        state_path=tmp_path / "fingerprints.json",
        runner=runner,
        dry_run=False,
    )

    assert (created, suppressed, duplicates) == (0, 0, 1)
    assert not any(call[:3] == ["gh", "issue", "create"] for call in runner.calls)


def test_open_github_duplicate_gets_comment_when_enabled(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:live:runtime:traceback:abc123",
        title="[LIVE] Runtime traceback in algo logs",
        component="runtime",
        reason="traceback",
        severity="high",
        count=2,
        first_seen="line:1",
        last_seen="line:2",
        classification="hard_error",
        snippets=["Traceback (most recent call last):", "NameError: name 'pos' is not defined"],
    )
    runner = FakeRunner(
        json.dumps(
            [
                {
                    "number": 123,
                    "title": finding.title,
                    "body": f"environment=live\nComponent: runtime\nReason: traceback\nFingerprint: {finding.fingerprint}",
                    "labels": [{"name": "algo-failure"}, {"name": "environment:live"}],
                }
            ]
        )
    )

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="live",
        lookback_minutes=30,
        duplicate_window_hours=24,
        state_path=tmp_path / "fingerprints.json",
        runner=runner,
        dry_run=False,
        github_comment_enabled=True,
    )

    assert (created, suppressed, duplicates) == (0, 0, 1)
    assert not any(call[:3] == ["gh", "issue", "create"] for call in runner.calls)
    assert any(call[:3] == ["gh", "issue", "comment"] and call[3] == "123" for call in runner.calls)
    assert "Log analyzer saw the same open issue fingerprint again." in runner.comment_bodies[0]
    assert "NameError: name 'pos' is not defined" in runner.comment_bodies[0]


def test_duplicate_match_by_component_reason_when_fingerprint_search_hits(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:live:entry_to_execution:entry_eval_pass_to_dispatch_skip",
        title="[LIVE] ENTRY_EVAL_PASS without downstream allocator or dispatch evidence",
        component="entry_to_execution",
        reason="entry_eval_pass_to_dispatch_skip",
        severity="medium",
        count=10,
        first_seen="line:1",
        last_seen="line:10",
        classification="pipeline_inconsistency",
    )
    runner = FakeRunner(
        json.dumps(
            [
                {
                    "number": 124,
                    "title": "Older equivalent issue",
                    "body": "environment=live\nComponent: entry_to_execution\nReason: entry_eval_pass_to_dispatch_skip",
                    "labels": [{"name": "algo-failure"}, {"name": "environment:live"}],
                }
            ]
        )
    )

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="live",
        lookback_minutes=30,
        duplicate_window_hours=24,
        state_path=tmp_path / "fingerprints.json",
        runner=runner,
        dry_run=False,
    )

    assert (created, suppressed, duplicates) == (0, 0, 1)


def test_noisy_unstable_quote_below_live_threshold_is_suppressed() -> None:
    logs = "\n".join(f"2026-06-29T10:{i:02d}:00 reason=unstable_quote symbol=ABC" for i in range(81))

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert findings == []


def test_spread_too_wide_count_six_suppressed_for_live_defaults() -> None:
    logs = "\n".join(f"2026-06-29T10:0{i}:00 reason=spread_too_wide symbol=ABC" for i in range(6))

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert findings == []


def test_entry_eval_pass_to_dispatch_skip_count_six_suppressed_for_live_defaults() -> None:
    logs = "\n".join(
        [
            f"2026-06-29T10:{i:02d}:00 ENTRY_EVAL_PASS symbol=SYM{i}"
            f"\n2026-06-29T10:{i:02d}:01 ORDER_SKIP symbol=SYM{i} reason=some_other_guard"
            for i in range(6)
        ]
    )

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert findings == []


def test_dynamic_relative_volume_after_entry_pass_is_expected_guard_skip() -> None:
    logs = "\n".join(
        [
            "2026-06-29T10:00:00 ENTRY_EVAL_PASS symbol=RPAY",
            "2026-06-29T10:00:01 ORDER_SKIP symbol=RPAY reason=dynamic_relative_volume",
        ]
    )

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert findings == []


def test_fcel_dynamic_spread_cap_after_entry_pass_is_expected_guard_skip() -> None:
    logs = "\n".join(
        [
            "2026-06-29T10:00:00 ENTRY_EVAL_PASS symbol=FCEL",
            "2026-06-29T10:00:01 ORDER_SKIP symbol=FCEL reason=dynamic_spread_cap source=capital_allocator",
            "2026-06-29T10:00:02 ALLOCATOR_DISPATCH_SKIPPED symbol=FCEL reason=dynamic_spread_cap",
        ]
    )

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert findings == []


def test_fcel_weak_catalyst_rvol_after_entry_pass_is_expected_guard_skip() -> None:
    logs = "\n".join(
        [
            "2026-06-29T10:00:00 ENTRY_EVAL_PASS symbol=FCEL",
            "2026-06-29T10:00:01 ORDER_SKIP symbol=FCEL reason=dynamic_weak_catalyst_relative_volume_below_0.50 source=capital_allocator",
            "2026-06-29T10:00:02 ALLOCATOR_DISPATCH_SKIPPED symbol=FCEL reason=dynamic_weak_catalyst_relative_volume_below_0.50",
        ]
    )

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert findings == []


def test_silent_entry_pass_without_downstream_evidence_creates_pipeline_issue() -> None:
    logs = "2026-06-29T10:00:00 ENTRY_EVAL_PASS symbol=FCEL"

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert len(findings) == 1
    assert findings[0].reason == "entry_eval_pass_to_dispatch_skip"
    assert findings[0].classification == "pipeline_inconsistency"


def test_dynamic_price_below_minimum_after_entry_pass_still_creates_issue() -> None:
    logs = "\n".join(
        [
            "2026-06-29T10:00:00 ENTRY_EVAL_PASS symbol=RPAY",
            "2026-06-29T10:00:01 ORDER_SKIP symbol=RPAY reason=dynamic_price_below_minimum",
        ]
    )

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert len(findings) == 1
    assert findings[0].reason == "dynamic_price_below_minimum_after_entry_pass"
    assert findings[0].classification == "pipeline_inconsistency"


def test_expected_strategy_skip_classification_and_threshold() -> None:
    logs = "\n".join(
        f"2026-06-29T10:{i:02d}:00 ORDER_SKIP symbol=ABC reason=weak_catalyst_dynamic_non_exceptional_live"
        for i in range(5)
    )

    findings = aal.analyze_log_text(logs, environment="live", thresholds=aal.DEFAULT_LIVE_THRESHOLDS)

    assert findings == []
    assert aal.classify_reason("weak_catalyst_dynamic_non_exceptional_live") == "expected_strategy_skip"


def test_processor_label_validation_only_applies_existing_processor_label(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:live:runtime:traceback",
        title="[LIVE] Runtime traceback in algo logs",
        component="runtime",
        reason="traceback",
        severity="high",
        count=1,
        first_seen="line:1",
        last_seen="line:1",
        classification="hard_error",
    )
    runner = FakeRunner(
        labels=["codex", "auto-fix", "algo-failure", "environment:live", "severity:high", "processor:live"]
    )

    aal.process_findings(
        [finding],
        environment="live",
        lookback_minutes=30,
        duplicate_window_hours=24,
        state_path=tmp_path / "fingerprints.json",
        runner=runner,
        dry_run=False,
        processor_label_enabled=True,
        processor_labels={"live": "processor:live"},
    )

    create = [call for call in runner.calls if call[:3] == ["gh", "issue", "create"]][0]
    assert "processor:live" in create


def test_generated_issue_body_includes_codex_instructions() -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:live:entry_to_execution:dynamic_relative_volume_after_entry_pass",
        title="[LIVE] dynamic_relative_volume after ENTRY_EVAL_PASS",
        component="entry_to_execution",
        reason="dynamic_relative_volume_after_entry_pass",
        severity="medium",
        count=1,
        first_seen="line:1",
        last_seen="line:2",
        classification="pipeline_inconsistency",
    )

    body = aal.build_issue_body(finding, environment="live", lookback_minutes=30)

    assert "Classification: pipeline_inconsistency" in body
    assert "## Codex instructions" in body
    assert "- git pull --rebase" in body
    assert "- add regression test" in body


def test_live_github_issue_enabled_creates_issue_for_hard_error(tmp_path: Path) -> None:
    write_analysis_config(tmp_path, github_issue_enabled=True)
    log_file = tmp_path / "live.log"
    log_file.write_text(
        "2026-06-29T10:01:01 Traceback (most recent call last):\n",
        encoding="utf-8",
    )
    runner = FakeRunner(
        labels=["codex", "auto-fix", "algo-failure", "environment:live", "severity:high"]
    )

    result = aal.run_analysis(
        root=tmp_path,
        environment="live",
        log_file=log_file,
        runner=runner,
        state_path=tmp_path / "fingerprints.json",
        timer=True,
    )

    assert result.created == 1
    create_calls = [call for call in runner.calls if call[:3] == ["gh", "issue", "create"]]
    assert len(create_calls) == 1
    create = create_calls[0]
    assert "environment:live" in create
    assert "severity:high" in create
    assert not any(call[:3] == ["gh", "issue", "comment"] for call in runner.calls)
    assert not any(call[:3] == ["gh", "issue", "close"] for call in runner.calls)
    assert not any("restart" in " ".join(call).lower() for call in runner.calls)


def test_live_github_issue_disabled_does_not_create_issue(tmp_path: Path) -> None:
    write_analysis_config(tmp_path, github_issue_enabled=False)
    log_file = tmp_path / "live.log"
    log_file.write_text(
        "2026-06-29T10:01:01 Traceback (most recent call last):\n",
        encoding="utf-8",
    )
    runner = FakeRunner()

    result = aal.run_analysis(
        root=tmp_path,
        environment="live",
        log_file=log_file,
        runner=runner,
        state_path=tmp_path / "fingerprints.json",
        timer=True,
    )

    assert len(result.findings) == 1
    assert result.created == 0
    assert not any(call[:3] == ["gh", "issue", "create"] for call in runner.calls)


def test_live_timer_with_github_enabled_suppresses_market_data_noise(tmp_path: Path) -> None:
    write_analysis_config(tmp_path, github_issue_enabled=True)
    log_file = tmp_path / "live.log"
    log_file.write_text(
        "\n".join(f"2026-06-29T10:{i % 60:02d}:00 reason=unstable_quote symbol=ABC" for i in range(200)),
        encoding="utf-8",
    )
    runner = FakeRunner()

    result = aal.run_analysis(
        root=tmp_path,
        environment="live",
        log_file=log_file,
        runner=runner,
        state_path=tmp_path / "fingerprints.json",
        timer=True,
    )

    assert len(result.findings) == 1
    assert result.findings[0].classification == "market_data_noise"
    assert result.created == 0
    assert result.suppressed == 1
    assert not any(call[:3] == ["gh", "issue", "create"] for call in runner.calls)


def test_analyze_fix_resolved(tmp_path: Path) -> None:
    log_file = tmp_path / "logs.txt"
    log_file.write_text("2026-06-29T10:00:00 INFO clean cycle\n", encoding="utf-8")
    runner = FakeRunner(
        issue_body=analyzer_issue_body()
    )

    result = aal.verify_fix(root=Path("."), issue_number=123, environment="live", log_file=log_file, runner=runner)

    assert result.status == "resolved"


def test_analyze_fix_still_occurring(tmp_path: Path) -> None:
    log_file = tmp_path / "logs.txt"
    log_file.write_text(
        "2026-06-29T10:00:00 ENTRY_EVAL_PASS symbol=RPAY\n"
        "2026-06-29T10:00:01 ORDER_SKIP symbol=RPAY reason=dynamic_price_below_minimum\n",
        encoding="utf-8",
    )
    runner = FakeRunner(
        issue_body=analyzer_issue_body()
    )

    result = aal.verify_fix(root=Path("."), issue_number=123, environment="live", log_file=log_file, runner=runner)

    assert result.status == "still_occurring"
    assert result.occurrences >= 1


def test_analyze_fix_inconclusive_without_fingerprint_or_reason(tmp_path: Path) -> None:
    log_file = tmp_path / "logs.txt"
    log_file.write_text("2026-06-29T10:00:00 INFO clean cycle\n", encoding="utf-8")
    runner = FakeRunner(issue_body="No analyzer metadata")

    result = aal.verify_fix(root=Path("."), issue_number=123, environment="live", log_file=log_file, runner=runner)

    assert result.status == "inconclusive"


def test_analyze_fix_comments_and_closes_resolved_analyzer_issue(tmp_path: Path) -> None:
    write_analysis_config(
        tmp_path,
        github_comment_enabled=True,
        auto_close_resolved_issues=True,
    )
    log_file = tmp_path / "logs.txt"
    log_file.write_text(
        "2026-06-30T10:00:00 ENTRY_EVAL_PASS symbol=RPAY\n"
        "2026-06-30T10:00:01 ALLOCATOR ACTIONS symbol=RPAY action=buy\n"
        "2026-06-30T10:00:02 ORDER_INTENT symbol=RPAY route=dynamic\n"
        "2026-06-30T10:00:03 ORDER_SUBMITTED symbol=RPAY order_id=abc\n"
        "2026-06-30T10:00:04 ALLOCATOR_DISPATCH_END symbol=RPAY result=submitted\n",
        encoding="utf-8",
    )
    runner = FakeRunner(
        issue_body=analyzer_issue_body(
            reason="entry_eval_pass_to_dispatch_skip",
            fingerprint="log-analysis:live:entry_to_execution:entry_eval_pass_to_dispatch_skip",
            snippet="ORDER_SKIP symbol=RPAY reason=dispatch_skip",
        )
    )

    result = aal.verify_fix(root=tmp_path, issue_number=123, environment="live", log_file=log_file, runner=runner)

    assert result.status == "resolved"
    assert "successful_downstream_path_detected" in result.details
    assert any(call[:3] == ["gh", "issue", "comment"] for call in runner.calls)
    assert any(call[:3] == ["gh", "issue", "close"] for call in runner.calls)
    assert "ORDER_SUBMITTED symbol=RPAY" in runner.comment_bodies[0]
    assert not any("restart" in " ".join(call).lower() for call in runner.calls)


def test_analyze_fix_does_not_close_unrelated_human_issue(tmp_path: Path) -> None:
    write_analysis_config(
        tmp_path,
        github_comment_enabled=True,
        auto_close_resolved_issues=True,
    )
    log_file = tmp_path / "logs.txt"
    log_file.write_text("2026-06-30T10:00:00 INFO clean cycle\n", encoding="utf-8")
    runner = FakeRunner(
        issue_body="- Reason: traceback\n- Fingerprint: log-analysis:live:runtime:traceback\n",
        issue_labels=["environment:live"],
    )

    result = aal.verify_fix(root=tmp_path, issue_number=123, environment="live", log_file=log_file, runner=runner)

    assert result.status == "inconclusive"
    assert result.details == "issue_not_matching_analyzer_metadata"
    assert not any(call[:3] == ["gh", "issue", "comment"] for call in runner.calls)
    assert not any(call[:3] == ["gh", "issue", "close"] for call in runner.calls)


def test_analyze_fix_false_config_prevents_comment_and_close(tmp_path: Path) -> None:
    write_analysis_config(
        tmp_path,
        github_comment_enabled=False,
        auto_close_resolved_issues=False,
    )
    log_file = tmp_path / "logs.txt"
    log_file.write_text("2026-06-30T10:00:00 INFO clean cycle\n", encoding="utf-8")
    runner = FakeRunner(issue_body=analyzer_issue_body(reason="traceback", fingerprint="log-analysis:live:runtime:traceback"))

    result = aal.verify_fix(root=tmp_path, issue_number=123, environment="live", log_file=log_file, runner=runner)

    assert result.status == "resolved"
    assert not any(call[:3] == ["gh", "issue", "comment"] for call in runner.calls)
    assert not any(call[:3] == ["gh", "issue", "close"] for call in runner.calls)


def test_analyze_fix_dry_run_prevents_comment_and_close(tmp_path: Path) -> None:
    write_analysis_config(
        tmp_path,
        github_comment_enabled=True,
        auto_close_resolved_issues=True,
    )
    log_file = tmp_path / "logs.txt"
    log_file.write_text("2026-06-30T10:00:00 INFO clean cycle\n", encoding="utf-8")
    runner = FakeRunner(issue_body=analyzer_issue_body(reason="traceback", fingerprint="log-analysis:live:runtime:traceback"))

    result = aal.verify_fix(
        root=tmp_path,
        issue_number=123,
        environment="live",
        log_file=log_file,
        runner=runner,
        dry_run=True,
    )

    assert result.status == "resolved"
    assert not any(call[:3] == ["gh", "issue", "comment"] for call in runner.calls)
    assert not any(call[:3] == ["gh", "issue", "close"] for call in runner.calls)


def test_analyze_fix_entry_issue_inconclusive_without_downstream_evidence(tmp_path: Path) -> None:
    write_analysis_config(
        tmp_path,
        github_comment_enabled=True,
        auto_close_resolved_issues=True,
    )
    log_file = tmp_path / "logs.txt"
    log_file.write_text("2026-06-30T10:00:00 ENTRY_EVAL_PASS symbol=RPAY\n", encoding="utf-8")
    runner = FakeRunner(
        issue_body=analyzer_issue_body(
            reason="entry_eval_pass_to_dispatch_skip",
            fingerprint="log-analysis:live:entry_to_execution:entry_eval_pass_to_dispatch_skip",
            snippet="ORDER_SKIP symbol=RPAY reason=dispatch_skip",
        )
    )

    result = aal.verify_fix(root=tmp_path, issue_number=123, environment="live", log_file=log_file, runner=runner)

    assert result.status == "still_occurring"
    assert result.details == "matching_finding_detected"
    assert not any(call[:3] == ["gh", "issue", "comment"] for call in runner.calls)
    assert not any(call[:3] == ["gh", "issue", "close"] for call in runner.calls)


def test_timer_mode_suppresses_low_severity_market_data_noise(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:live:market_data:unstable_quote",
        title="[LIVE] Repeated unstable quote blocks",
        component="market_data",
        reason="unstable_quote",
        severity="low",
        count=200,
        first_seen="line:1",
        last_seen="line:200",
        classification="market_data_noise",
    )

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="live",
        lookback_minutes=30,
        duplicate_window_hours=24,
        state_path=tmp_path / "fingerprints.json",
        runner=FakeRunner(),
        dry_run=False,
        timer=True,
    )

    assert (created, suppressed, duplicates) == (0, 1, 0)


def test_market_data_noise_remains_locally_suppressed(tmp_path: Path) -> None:
    finding = aal.LogFinding(
        fingerprint="log-analysis:paper:market_data:unstable_quote",
        title="[PAPER] Repeated unstable quote blocks",
        component="market_data",
        reason="unstable_quote",
        severity="low",
        count=10,
        first_seen="line:1",
        last_seen="line:10",
        classification="market_data_noise",
    )
    state_path = tmp_path / "fingerprints.json"
    state_path.write_text(
        json.dumps(
            {
                "fingerprints": {
                    finding.fingerprint: {
                        "last_seen": datetime.now(timezone.utc).isoformat(),
                        "seen_count": 1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    runner = FakeRunner()

    created, suppressed, duplicates = aal.process_findings(
        [finding],
        environment="paper",
        lookback_minutes=15,
        duplicate_window_hours=24,
        state_path=state_path,
        runner=runner,
        dry_run=False,
    )

    assert (created, suppressed, duplicates) == (0, 1, 0)
    assert not any(call[:3] == ["gh", "issue", "create"] for call in runner.calls)
