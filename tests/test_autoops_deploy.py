from __future__ import annotations

from pathlib import Path

from scripts import run_autoops


def _write_autoops_config(
    root: Path,
    *,
    deploy: bool = True,
    restart: bool = True,
    verify: bool = True,
) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "default.yaml").write_text(
        "\n".join(
            [
                "autoops:",
                f"  live_auto_deploy_enabled: {str(deploy).lower()}",
                f"  live_auto_restart_enabled: {str(restart).lower()}",
                f"  live_post_deploy_verify_enabled: {str(verify).lower()}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _patch_host_and_sleep(monkeypatch, host: str = run_autoops.LIVE_DEPLOY_HOSTNAME) -> None:
    monkeypatch.setattr(run_autoops.socket, "gethostname", lambda: host)
    monkeypatch.setattr(run_autoops.time, "sleep", lambda _seconds: None)


def test_deploy_disabled_does_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path, deploy=False)
    _patch_host_and_sleep(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: calls.append(list(args)) or (0, ""))

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 1
    assert calls == []
    assert "reason=deploy_disabled" in capsys.readouterr().out


def test_wrong_hostname_blocks_deploy(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch, host="not-algosphere-live-host")
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: calls.append(list(args)) or (0, ""))

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 1
    assert calls == []
    assert "reason=wrong_hostname" in capsys.readouterr().out


def test_paper_environment_blocks_deploy(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: calls.append(list(args)) or (0, ""))

    rc = run_autoops._deploy_latest(tmp_path, environment="paper")

    assert rc == 1
    assert calls == []
    assert "reason=live_only" in capsys.readouterr().out


def test_dirty_git_tree_blocks_deploy(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["git", "status", "--porcelain"]:
            return 0, " M scripts/run_autoops.py"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 1
    assert calls == [["git", "status", "--porcelain"]]
    assert "reason=dirty_git_tree" in capsys.readouterr().out


def test_git_pull_failure_blocks_restart(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["git", "pull", "--rebase"]:
            return 1, "conflict"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 1
    assert ["sudo", "-n", run_autoops.SYSTEMCTL, "restart", "algo.service"] not in calls
    assert "reason=git_pull_failed" in capsys.readouterr().out


def test_passwordless_sudo_missing_blocks_restart(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(run_autoops.getpass, "getuser", lambda: "algosphere")

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["sudo", "-n", "true"]:
            return 1, "sudo: a password is required"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    out = capsys.readouterr().out
    assert rc == 1
    assert "reason=passwordless_sudo_not_configured" in out
    assert "sudo visudo -f /etc/sudoers.d/algo-autoops" in out
    assert "algosphere ALL=(root) NOPASSWD: /usr/bin/systemctl restart algo.service" in out
    assert ["sudo", "-n", run_autoops.SYSTEMCTL, "restart", "algo.service"] not in calls


def test_restart_failure_reports_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)

    def fake_run(args, **kwargs):
        if args[:3] == ["sudo", "-n", "true"]:
            return 0, ""
        if args[:4] == ["sudo", "-n", run_autoops.SYSTEMCTL, "restart"]:
            return 1, "restart failed"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 1
    assert "reason=restart_failed" in capsys.readouterr().out


def test_analyzer_hard_error_after_restart_reports_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)

    def fake_run(args, **kwargs):
        if args[:3] == ["sudo", "-n", "true"]:
            return 0, ""
        if args[:4] == ["sudo", "-n", run_autoops.SYSTEMCTL, "is-active"]:
            return 0, "active"
        if args and (str(args[0]).endswith("python") or str(args[0]).endswith("python3")):
            return 0, "ISSUE_ROUTING env=live classification=hard_error fingerprint=x"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 1
    assert "reason=analyzer_hard_error" in capsys.readouterr().out


def test_service_inactive_uses_sudo_noninteractive_status(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["sudo", "-n", "true"]:
            return 0, ""
        if args[:4] == ["sudo", "-n", run_autoops.SYSTEMCTL, "is-active"]:
            return 3, "inactive"
        if args[:4] == ["sudo", "-n", run_autoops.SYSTEMCTL, "status"]:
            return 3, "algo.service inactive"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 1
    assert ["sudo", "-n", run_autoops.SYSTEMCTL, "status", "algo.service"] in calls
    assert not any(call and call[0] == "sudo" and "-n" not in call for call in calls)
    assert "reason=service_inactive" in capsys.readouterr().out


def test_success_path_runs_pull_restart_health_check_and_analyzer(tmp_path: Path, monkeypatch, capsys) -> None:
    _write_autoops_config(tmp_path)
    _patch_host_and_sleep(monkeypatch)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args[:3] == ["sudo", "-n", "true"]:
            return 0, ""
        if args[:4] == ["sudo", "-n", run_autoops.SYSTEMCTL, "is-active"]:
            return 0, "active"
        if "scripts/analyze_algo_logs.py" in args:
            return 0, "LOG_ANALYZER env=live dry_run=true\nissues detected=0"
        return 0, ""

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops._deploy_latest(tmp_path, environment="live")

    assert rc == 0
    assert ["git", "status", "--porcelain"] in calls
    assert ["git", "pull", "--rebase"] in calls
    assert ["sudo", "-n", "true"] in calls
    assert ["sudo", "-n", run_autoops.SYSTEMCTL, "restart", "algo.service"] in calls
    assert ["sudo", "-n", run_autoops.SYSTEMCTL, "is-active", "algo.service"] in calls
    assert not any(call and call[0] == "sudo" and "-n" not in call for call in calls)
    analyzer_calls = [call for call in calls if "scripts/analyze_algo_logs.py" in call]
    assert analyzer_calls
    assert "--dry-run" in analyzer_calls[0]
    assert "AUTOOPS_DEPLOY_STATUS success=true" in capsys.readouterr().out
