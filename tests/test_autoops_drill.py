from __future__ import annotations

from pathlib import Path

from scripts import run_autoops


def _seed_root(root: Path) -> None:
    for rel in run_autoops.REQUIRED_AUTOOPS_PATHS:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test\n", encoding="utf-8")
        if rel.endswith(".sh"):
            path.chmod(path.stat().st_mode | 0o111)


def test_dry_run_drill_emits_issue_payload_but_does_not_create_real_issue(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _seed_root(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_run(args, *, cwd=run_autoops.PROJECT_ROOT, timeout=5.0):
        calls.append(tuple(args))
        return 0, "gh version 2.0.0"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops.main(["drill", "--live", "--dry-run", "--project-root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOOPS_HEALTH_FAILURE_DETECTED dry_run=true simulated=true" in out
    assert "AUTOOPS_ISSUE_PAYLOAD dry_run=true generated=true" in out
    assert "AUTOOPS_ISSUE_CREATED dry_run=true skipped_github_write=true" in out
    assert not any(call[:3] == ("gh", "issue", "create") for call in calls)


def test_dry_run_labels_contain_environment_and_processor(tmp_path: Path, capsys) -> None:
    _seed_root(tmp_path)

    rc = run_autoops.main(["drill", "--live", "--dry-run", "--project-root", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "labels=live,codex,auto-fix,environment:live,processor:live-linux" in out
    assert "AUTOOPS_CODEX_PROCESSOR dry_run=true would_accept_issue=true" in out
    assert "AUTOOPS_PR_VALIDATION dry_run=true required=true" in out


def test_restart_is_never_executed_in_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    calls: list[tuple[str, ...]] = []

    def fake_run(args, *, cwd=run_autoops.PROJECT_ROOT, timeout=5.0):
        calls.append(tuple(args))
        return 0, "ok"

    monkeypatch.setattr(run_autoops, "_run", fake_run)

    rc = run_autoops.main(["drill", "--live", "--dry-run", "--project-root", str(tmp_path)])

    assert rc == 0
    assert "AUTOOPS_RESTART_GATED dry_run=true executed=false" in capsys.readouterr().out
    assert not any("restart" in " ".join(call) for call in calls)
