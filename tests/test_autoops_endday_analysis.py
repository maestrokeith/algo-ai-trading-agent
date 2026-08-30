from __future__ import annotations

import json
from pathlib import Path

from scripts import run_autoops


def _write_autoops_config(
    root: Path,
    *,
    analysis: bool = True,
    issue: bool = True,
    codex: bool = True,
    autostart: bool = True,
) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "default.yaml").write_text(
        "\n".join(
            [
                "autoops:",
                f"  live_end_day_analysis_enabled: {str(analysis).lower()}",
                f"  live_end_day_issue_enabled: {str(issue).lower()}",
                f"  live_end_day_codex_enabled: {str(codex).lower()}",
                f"  live_end_day_codex_autostart_enabled: {str(autostart).lower()}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _patch_live(monkeypatch) -> None:
    monkeypatch.setattr(run_autoops.socket, "gethostname", lambda: run_autoops.LIVE_DEPLOY_HOSTNAME)
    monkeypatch.setattr(run_autoops, "_today_local", lambda: "2026-06-30")
    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: f"/usr/bin/{name}")


def _label_list(labels: list[str] | tuple[str, ...] = run_autoops.REQUIRED_GITHUB_LABELS) -> str:
    return json.dumps([{"name": label} for label in labels])


def test_endday_command_blocked_for_paper(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: calls.append(list(args)) or (0, ""))

    rc = run_autoops._end_day_analysis(tmp_path, environment="paper")

    assert rc == 1
    assert calls == []
    assert "reason=live_only" in capsys.readouterr().out


def test_endday_disabled_config_does_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path, analysis=False)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: calls.append(list(args)) or (0, ""))

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert calls == []
    assert "reason=disabled" in capsys.readouterr().out


def test_endday_runs_expected_commands_in_order_when_clean(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return 0, "clean"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    algo = str(tmp_path / "bin" / "algo")
    assert rc == 0
    assert calls == [
        [algo, "capture-metrics", "--end-day", "--live"],
        [algo, "end-day", "--live"],
        [algo, "research-feedback", "2026-06-30", "--user", "live_bot"],
        [run_autoops.sys.executable, "scripts/check_positions.py"],
        [run_autoops.sys.executable, "scripts/analyze_algo_logs.py", "--live", "--lookback-minutes", "390"],
    ]
    out = capsys.readouterr().out
    assert "actionable=false" in out
    assert "issue_created=false" in out


def test_endday_suppressed_only_log_analyzer_findings_are_not_actionable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    analyzer_output = "\n".join(
        [
            "LOG_ANALYZER_LOG_SOURCE source=journalctl",
            "ISSUE_SUPPRESSED env=live reason=local_duplicate fingerprint=log-analysis:live:market_data:unstable_quote",
            "ISSUE_SUPPRESSED env=live reason=local_duplicate fingerprint=log-analysis:live:market_data:spread_too_wide",
            "LOG_ANALYZER env=live dry_run=false",
            "issues detected=2",
            "issues suppressed=2",
            "duplicates ignored=0",
            "GitHub issue created=0",
            "LOG_ANALYZER_FINDING fingerprint=log-analysis:live:market_data:unstable_quote classification=market_data_noise count=411 title=[LIVE] Repeated unstable quote blocks",
            "LOG_ANALYZER_FINDING fingerprint=log-analysis:live:market_data:spread_too_wide classification=market_data_noise count=92 title=[LIVE] Repeated spread_too_wide blocks",
        ]
    )

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, analyzer_output
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert not any(call[:3] == ["gh", "issue", "create"] for call in calls)
    assert not any("process_codex_issues_local.sh" in " ".join(call) for call in calls)
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_STATUS success=true actionable=false issue_created=false" in out
    assert "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=no_actionable_issues" in out


def test_endday_issue_created_launches_codex(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    issue_body = ""
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        nonlocal issue_body
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list()
        if argv[:3] == ["gh", "issue", "create"]:
            issue_body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
            return 0, "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/999"
        if argv[:3] == ["gh", "issue", "edit"]:
            return 0, "edited"
        if argv == [str(processor), "--live"]:
            return 0, "processor ok"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, "issues detected=1\nISSUE_ROUTING env=live classification=pipeline_inconsistency"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    issue_calls = [call for call in calls if call[:3] == ["gh", "issue", "create"]]
    assert len(issue_calls) == 1
    issue_call = issue_calls[0]
    for label in ("codex", "auto-fix", "algo-failure", "environment:live", "processor:live-linux", "severity:medium"):
        assert label in issue_call
    assert ["gh", "issue", "edit", "999", "--add-label", "processor:live-linux"] not in calls
    assert "ISSUE_ROUTING env=live" in issue_body
    assert "- git pull --rebase" in issue_body
    assert "- add regression test" in issue_body
    assert calls.count([str(processor), "--live"]) == 1
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_ISSUE_CREATED" in out
    assert "AUTOOPS_ENDDAY_CODEX_START issue_count=1 issue_numbers=999" in out
    assert "AUTOOPS_ENDDAY_CODEX_COMPLETE issue_count=1 issue_numbers=999 processor_exit_code=0" in out


def test_endday_processor_label_missing_blocks_issue_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    labels_without_processor = tuple(label for label in run_autoops.REQUIRED_GITHUB_LABELS if label != "processor:live-linux")
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list(labels_without_processor)
        if argv[:3] == ["gh", "label", "create"]:
            return 1, "could not create label"
        if argv[:3] == ["gh", "issue", "create"]:
            assert "processor:live-linux" in argv
            return 0, "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/1002"
        if argv[:3] == ["gh", "issue", "edit"]:
            raise AssertionError("optional processor label should not be applied when unavailable")
        if argv == [str(processor), "--live"]:
            return 0, "processor ok"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, "issues detected=1"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 1
    assert [str(processor), "--live"] not in calls
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_STATUS success=false reason=github_issue_failed" in out
    assert "missing_required_labels=processor:live-linux" in out


def test_endday_core_label_missing_still_fails_issue_creation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    labels_without_core = tuple(label for label in run_autoops.REQUIRED_GITHUB_LABELS if label != "algo-failure")
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list(labels_without_core)
        if argv[:3] == ["gh", "label", "create"] and argv[3] == "algo-failure":
            return 1, "could not create core label"
        if argv[:3] == ["gh", "issue", "create"]:
            return 1, "could not add label: 'algo-failure' not found"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, "issues detected=1"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 1
    assert not any("process_codex_issues_local.sh" in " ".join(call) for call in calls)
    out = capsys.readouterr().out
    assert "AUTOOPS_GITHUB_LABEL_CREATE_FAILED name=algo-failure" in out
    assert "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=github_issue_failed" in out


def test_endday_analyzer_duplicate_succeeds_and_launches_codex(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    analyzer_output = "\n".join(
        [
            "LOG_ANALYZER env=live dry_run=false",
            "issues detected=1",
            "ISSUE_DUPLICATE existing_issue=175 fingerprint=log-analysis:live:runtime:traceback:b46f346628",
            "GitHub issue created=0",
        ]
    )

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:3] == ["gh", "issue", "view"] and argv[3] == "175":
            return 0, json.dumps(
                {
                    "number": 175,
                    "state": "OPEN",
                    "labels": [
                        {"name": "codex"},
                        {"name": "auto-fix"},
                        {"name": "environment:live"},
                        {"name": "algo-failure"},
                    ],
                }
            )
        if argv == [str(processor), "--live"]:
            return 0, "processor ok"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, analyzer_output
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert not any(call[:3] == ["gh", "issue", "create"] for call in calls)
    assert [str(processor), "--live"] in calls
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_STATUS success=true actionable=true issue_created=false duplicate_issue=true issue_numbers=175" in out
    assert "AUTOOPS_ENDDAY_CODEX_START issue_count=1 issue_numbers=175" in out


def test_endday_analyzer_created_issue_skips_wrapper_issue_and_launches_codex(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    analyzer_output = "\n".join(
        [
            "LOG_ANALYZER env=live dry_run=false",
            "issues detected=4",
            "ISSUE_ROUTING env=live classification=hard_error fingerprint=log-analysis:live:runtime:traceback:b46f346628",
            "ISSUE_CREATED env=live issue=178 classification=hard_error severity=high fingerprint=log-analysis:live:runtime:traceback:b46f346628",
            "ISSUE_DUPLICATE existing_issue=175 fingerprint=log-analysis:live:runtime:exception:1c8c1b0272",
        ]
    )

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:3] == ["gh", "issue", "view"] and argv[3] in {"175", "178"}:
            return 0, json.dumps(
                {
                    "number": int(argv[3]),
                    "state": "OPEN",
                    "labels": [
                        {"name": "codex"},
                        {"name": "auto-fix"},
                        {"name": "environment:live"},
                        {"name": "algo-failure"},
                    ],
                }
            )
        if argv == [str(processor), "--live"]:
            return 0, "processor ok"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, analyzer_output
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert not any(call[:3] == ["gh", "issue", "create"] for call in calls)
    assert [str(processor), "--live"] in calls
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_STATUS success=true actionable=true issue_created=false analyzer_issue=true issue_numbers=178,175" in out
    assert "AUTOOPS_ENDDAY_CODEX_START issue_count=2 issue_numbers=178,175" in out


def test_endday_runtime_traceback_creates_high_severity_issue_and_launches_codex(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    issue_body = ""
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    analyzer_output = "\n".join(
        [
            "LOG_ANALYZER_LOG_SOURCE source=journalctl",
            "ISSUE_ROUTING env=live classification=hard_error fingerprint=log-analysis:live:runtime:traceback:b46f346628",
            "LOG_ANALYZER_FINDING fingerprint=log-analysis:live:runtime:traceback:b46f346628 classification=hard_error count=5 title=[LIVE] Runtime traceback in algo logs",
            "Jun 30 15:24:23 algosphere-live-host python3.12[1643026]: Traceback (most recent call last):",
            "Jun 30 15:24:23 algosphere-live-host python3.12[1643026]: ERROR: NameError: name 'pos' is not defined - skipping to next user",
        ]
    )

    def fake_run(args, **kwargs):
        nonlocal issue_body
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list()
        if argv[:3] == ["gh", "issue", "create"]:
            issue_body = Path(argv[argv.index("--body-file") + 1]).read_text(encoding="utf-8")
            return 0, "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/1001"
        if argv[:3] == ["gh", "issue", "edit"]:
            return 0, "edited"
        if argv == [str(processor), "--live"]:
            return 0, "processor ok"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, analyzer_output
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    issue_call = [call for call in calls if call[:3] == ["gh", "issue", "create"]][0]
    for label in ("codex", "auto-fix", "algo-failure", "environment:live", "processor:live-linux", "severity:high"):
        assert label in issue_call
    assert ["gh", "issue", "edit", "1001", "--add-label", "processor:live-linux"] not in calls
    assert "NameError: name 'pos' is not defined" in issue_body
    assert "classification=hard_error" in issue_body
    assert calls.count([str(processor), "--live"]) == 1
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_ISSUE_CREATED" in out
    assert "AUTOOPS_ENDDAY_CODEX_START issue_count=1 issue_numbers=1001" in out


def test_endday_codex_autostart_disabled_stops_after_issue(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path, autostart=False)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list()
        if argv[:3] == ["gh", "issue", "create"]:
            return 0, "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/1000"
        if argv[:3] == ["gh", "issue", "edit"]:
            return 0, "edited"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, "issues detected=1"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert not any(call[-1:] == ["--live"] and "process_codex_issues_local.sh" in call[0] for call in calls)
    assert "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=codex_autostart_disabled" in capsys.readouterr().out


def test_endday_no_issue_created_when_clean(tmp_path: Path, monkeypatch) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: calls.append(list(args)) or (0, "clean"))

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert not any(call[:3] == ["gh", "issue", "create"] for call in calls)


def test_endday_dry_run_creates_no_issue(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if "scripts/analyze_algo_logs.py" in args:
            return 0, "issues detected=1"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live", dry_run=True)

    assert rc == 1
    assert not any(call[:3] == ["gh", "issue", "create"] for call in calls)
    assert not any("process_codex_issues_local.sh" in " ".join(call) for call in calls)
    analyzer = [call for call in calls if "scripts/analyze_algo_logs.py" in call][0]
    assert "--dry-run" in analyzer
    end_day = [call for call in calls if call[:3] == [str(tmp_path / "bin" / "algo"), "end-day", "--live"]][0]
    assert "--dry-run" not in end_day
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_DRYRUN_SKIP command=end-day reason=no_dry_run_support" in out
    assert "issue_created=false" in out


def test_endday_github_failure_skips_codex(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list()
        if argv[:3] == ["gh", "issue", "create"]:
            return 1, "gh failed"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, "issues detected=1"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 1
    assert not any("process_codex_issues_local.sh" in " ".join(call) for call in calls)
    assert "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=github_issue_failed" in capsys.readouterr().out


def test_endday_codex_already_running_skips_processor(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_active_codex_locks", lambda: [Path("/tmp/algo_codex_issue_999.lock")])

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list()
        if argv[:3] == ["gh", "issue", "create"]:
            return 0, "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/999"
        if argv[:3] == ["gh", "issue", "edit"]:
            return 0, "edited"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, "issues detected=1"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert not any("process_codex_issues_local.sh" in " ".join(call) for call in calls)
    assert "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=codex_already_running" in capsys.readouterr().out


def test_endday_processor_failure_reports_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list()
        if argv[:3] == ["gh", "issue", "create"]:
            return 0, "https://github.com/YOUR_GITHUB_ORG/algo-ai-trading-agent/issues/999"
        if argv[:3] == ["gh", "issue", "edit"]:
            return 0, "edited"
        if argv == [str(processor), "--live"]:
            return 2, "processor failed"
        if "scripts/analyze_algo_logs.py" in argv:
            return 0, "issues detected=1"
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 1
    assert [str(processor), "--live"] in calls
    assert "AUTOOPS_ENDDAY_CODEX_COMPLETE issue_count=1 issue_numbers=999 processor_exit_code=2" in capsys.readouterr().out


def test_endday_codex_auth_preflight_failure_skips_processor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:3] == ["gh", "api", "graphql"]:
            return 1, "HTTP 401: Bad credentials (https://api.github.com/graphql)"
        return 0, "ok"

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._autostart_endday_codex(tmp_path, issue_numbers=[167, 175])

    assert rc == 1
    assert [str(processor), "--live"] not in calls
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_CODEX_SKIPPED reason=github_auth_failed" in out
    assert "reason=bad_credentials_graphql" in out
    assert "gh auth status" in out
    assert "systemctl --user import-environment GH_TOKEN GITHUB_TOKEN" in out


def test_endday_processor_http_401_output_is_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    processor = tmp_path / "scripts" / "process_codex_issues_local.sh"
    processor.write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv == [str(processor), "--live"]:
            return 0, "\n".join(
                [
                    "HTTP 401: Bad credentials (https://api.github.com/graphql)",
                    "Try authenticating with: gh auth login",
                    "No eligible Codex auto-fix issues found.",
                ]
            )
        return 0, "ok"

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._autostart_endday_codex(tmp_path, issue_numbers=[167])

    assert rc == 1
    out = capsys.readouterr().out
    assert "AUTOOPS_ENDDAY_CODEX_COMPLETE issue_count=1 issue_numbers=167 processor_exit_code=0 auth_error=true" in out
    assert "No eligible Codex auto-fix issues found." in out


def test_endday_auth_guidance_does_not_print_token_values(monkeypatch, capsys) -> None:
    monkeypatch.setenv("GH_TOKEN", "secret-token-value")

    print(run_autoops._github_auth_guidance())

    out = capsys.readouterr().out
    assert "secret-token-value" not in out
    assert "GH_TOKEN=..." in out


def test_endday_timer_install_writes_expected_unit_and_timer(tmp_path: Path, monkeypatch, capsys) -> None:
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list()
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)
    user_dir = tmp_path / "systemd-user"

    rc = run_autoops._install_endday_timer(tmp_path, environment="live", user_systemd_dir=user_dir)

    service = (user_dir / "algosphere-live-endday-analysis.service").read_text(encoding="utf-8")
    timer = (user_dir / "algosphere-live-endday-analysis.timer").read_text(encoding="utf-8")
    assert rc == 0
    assert "WorkingDirectory=/opt/algosphere/algo-ai-trading-agent" in service
    assert "EnvironmentFile=-%h/.config/algosphere/github.env" in service
    assert "ExecStart=/opt/algosphere/algo-ai-trading-agent/bin/algo autoops end-day-analysis --live" in service
    assert "OnCalendar=Mon..Fri *-*-* 17:30:00 America/New_York" in timer
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "algosphere-live-endday-analysis.timer"] in calls
    assert "journalctl --user -u algosphere-live-endday-analysis.service" in capsys.readouterr().out


def test_endday_timer_install_bootstraps_live_linux_processor_label(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    labels_without_processor = tuple(label for label in run_autoops.REQUIRED_GITHUB_LABELS if label != "processor:live-linux")

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:4] == ["gh", "label", "list", "--json"]:
            return 0, _label_list(labels_without_processor)
        if argv[:3] == ["gh", "label", "create"]:
            return 0, "created"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._install_endday_timer(tmp_path, environment="live", user_systemd_dir=tmp_path / "systemd-user")

    assert rc == 0
    create_calls = [call for call in calls if call[:4] == ["gh", "label", "create", "processor:live-linux"]]
    assert len(create_calls) == 1
    assert "--description" in create_calls[0]
    assert "Live Linux Codex processor" in create_calls[0]
    assert "Configured time: 17:30 ET Monday-Friday" in capsys.readouterr().out


def test_endday_path_never_restarts_service(tmp_path: Path, monkeypatch) -> None:
    _write_autoops_config(tmp_path)
    _patch_live(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: calls.append(list(args)) or (0, "clean"))

    rc = run_autoops._end_day_analysis(tmp_path, environment="live")

    assert rc == 0
    assert not any("restart" in call for call in calls)
    assert not any("systemctl" in call for call in calls)
