from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEALTH_SCRIPT = PROJECT_ROOT / "scripts" / "check_paper_options_health.sh"
LOOP_SCRIPT = PROJECT_ROOT / "scripts" / "run_paper_options_stabilization_loop.sh"


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _seed_repo(tmp_path: Path, *, algo_script: str, paper_log: str = "") -> tuple[Path, Path]:
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    _write_executable(root / "bin" / "algo", algo_script)
    log_path = root / "data" / "review" / "2026-06-14" / "paper_full.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(paper_log, encoding="utf-8")
    return root, log_path


def _healthy_algo() -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "echo 'OPTIONS_CONFIG enabled=true mode=paper_only'",
            "echo 'OPTION_SIGNAL symbol=QQQ underlying=QQQ direction=bullish'",
            "echo 'OPTION_CHAIN_LOADED symbol=QQQ right=call chain_rows=4 path=ranked_budget'",
            "echo 'OPTION_SELECTED symbol=QQQ right=call contract=QQQ260630C00350000'",
            "echo 'PASS paper options diagnostics user=paper_bot symbol=QQQ chain_source=mock options_placed=0'",
            "exit 0",
            "",
        ]
    )


def _failing_algo() -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "echo 'PAPER_OPTIONS_DIAGNOSTICS_FAILED RuntimeError: mock failure' >&2",
            "exit 2",
            "",
        ]
    )


def _run_health(
    root: Path,
    state_dir: Path,
    *args: str,
    path: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ALGO_REPO_ROOT": str(root),
        "PAPER_OPTIONS_HEALTH_STATE_DIR": str(state_dir),
    }
    if extra_env:
        env.update(extra_env)
    if path is not None:
        env["PATH"] = path
    return subprocess.run(
        [str(HEALTH_SCRIPT), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def _run_loop(
    root: Path,
    state_dir: Path,
    *args: str,
    fake_bin: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ALGO_REPO_ROOT": str(root),
        "PAPER_OPTIONS_HEALTH_SCRIPT": str(HEALTH_SCRIPT),
        "PAPER_OPTIONS_HEALTH_STATE_DIR": str(state_dir / "health"),
        "PAPER_OPTIONS_STABILIZATION_STATE_DIR": str(state_dir / "loop"),
    }
    if fake_bin is not None:
        env["PATH"] = f"{fake_bin}:{os.environ['PATH']}"
    return subprocess.run(
        [str(LOOP_SCRIPT), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )


def _fake_gh(tmp_path: Path, script: str) -> Path:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "gh", script)
    _write_executable(fake_bin / "hostname", "#!/usr/bin/env bash\necho paper-mac\n")
    return fake_bin


def test_health_script_exists_and_is_executable() -> None:
    assert HEALTH_SCRIPT.exists()
    assert HEALTH_SCRIPT.stat().st_mode & 0o111
    assert LOOP_SCRIPT.exists()
    assert LOOP_SCRIPT.stat().st_mode & 0o111


def test_healthy_paper_options_reports_stable_after_required_ticks(tmp_path: Path) -> None:
    root, _log = _seed_repo(tmp_path, algo_script=_healthy_algo())
    state_dir = tmp_path / "state"

    first = _run_health(root, state_dir, "--env", "paper", "--required-stable-ticks", "3")
    second = _run_health(root, state_dir, "--env", "paper", "--required-stable-ticks", "3")
    third = _run_health(root, state_dir, "--env", "paper", "--required-stable-ticks", "3")

    assert first.returncode == 0
    assert "PAPER_OPTIONS_HEALTH status=healthy" in first.stdout
    assert "PAPER_OPTIONS_HEALTH stable_ticks=1" in first.stdout
    assert "PAPER_OPTIONS_HEALTH stable_ticks=2" in second.stdout
    assert "PAPER_OPTIONS_HEALTH stable_ticks=3" in third.stdout
    assert "PAPER_OPTIONS_HEALTH required_stable_ticks=3" in third.stdout


def test_paper_options_health_uses_dated_review_log_not_stale_log(tmp_path: Path) -> None:
    root, _log = _seed_repo(tmp_path, algo_script=_healthy_algo())
    stale = root / "data" / "review" / "2026-06-25" / "paper_full.log"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("Traceback stale options log\n", encoding="utf-8")
    today = root / "data" / "review" / "2026-06-28" / "paper_full.log"
    today.parent.mkdir(parents=True, exist_ok=True)
    today.write_text("OPTION_SELECTED symbol=QQQ contract=QQQ260630C00350000\n", encoding="utf-8")

    proc = _run_health(
        root,
        tmp_path / "state",
        "--env",
        "paper",
        extra_env={"ALGO_HEALTH_DATE": "2026-06-28"},
    )

    assert proc.returncode == 0, proc.stderr
    assert f"PAPER_OPTIONS_HEALTH log={today}" in proc.stdout
    assert str(stale) not in proc.stdout


def test_unhealthy_paper_options_creates_issue_with_paper_labels_and_title(tmp_path: Path) -> None:
    root, _log = _seed_repo(tmp_path, algo_script=_failing_algo())
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_gh(
        tmp_path,
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f"echo \"$@\" >> {calls}",
                "if [[ \"$1 $2\" == 'issue list' ]]; then printf '[]\\n'; exit 0; fi",
                "if [[ \"$1 $2\" == 'issue create' ]]; then exit 0; fi",
                "exit 0",
                "",
            ]
        ),
    )

    proc = _run_loop(root, tmp_path / "state", "--env", "paper", fake_bin=fake_bin)

    assert proc.returncode == 1
    assert "PAPER_OPTIONS_HEALTH status=unhealthy" in proc.stdout
    assert "PAPER_OPTIONS_STABILIZATION status=repair_issue_ready" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title PAPER_OPTIONS [PAPER] unstable: diagnostics_failed" in gh_calls
    for label in (
        "codex",
        "auto-fix",
        "algo-health",
        "environment:paper",
        "processor:mac-paper",
        "paper-options",
    ):
        assert f"--label {label}" in gh_calls
    body_file = gh_calls.split("--body-file ", 1)[1].split()[0]
    body = Path(body_file).read_text(encoding="utf-8")
    assert "environment=paper" in body
    assert "No live trading changes." in body
    assert "PYTHONPATH=. pytest tests/test_paper_options_health.py -v" in body


def test_duplicate_issue_detection_skips_create(tmp_path: Path) -> None:
    root, _log = _seed_repo(tmp_path, algo_script=_failing_algo())
    calls = tmp_path / "gh_calls.log"
    fake_bin = _fake_gh(
        tmp_path,
        "\n".join(
            [
                "#!/usr/bin/env bash",
                f"echo \"$@\" >> {calls}",
                "if [[ \"$1 $2\" == 'issue list' ]]; then printf '[{\"body\":\"paper-options:diagnostics_failed\"}]\\n'; exit 0; fi",
                "if [[ \"$1 $2\" == 'issue create' ]]; then exit 42; fi",
                "exit 0",
                "",
            ]
        ),
    )

    proc = _run_loop(root, tmp_path / "state", "--env", "paper", fake_bin=fake_bin)

    assert proc.returncode == 1
    assert "Existing PAPER_OPTIONS issue found for paper-options:diagnostics_failed" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "issue list" in gh_calls
    assert "issue create" not in gh_calls


def test_max_attempts_stops_loop(tmp_path: Path) -> None:
    root, _log = _seed_repo(tmp_path, algo_script=_failing_algo())
    state = tmp_path / "state"
    attempts = state / "loop" / f"attempts_"
    attempts.parent.mkdir(parents=True)
    today = subprocess.check_output(["date", "-u", "+%Y-%m-%d"], text=True).strip()
    (attempts.parent / f"attempts_{today}").write_text("3\n", encoding="utf-8")

    proc = _run_loop(root, state, "--env", "paper", "--max-attempts", "3")

    assert proc.returncode == 1
    assert "PAPER_OPTIONS_STABILIZATION status=needs_human_review" in proc.stdout


def test_live_environment_is_rejected(tmp_path: Path) -> None:
    root, _log = _seed_repo(tmp_path, algo_script=_healthy_algo())

    health = _run_health(root, tmp_path / "state", "--env", "live")
    loop = _run_loop(root, tmp_path / "state2", "--env", "live")

    assert health.returncode == 2
    assert "paper-only" in health.stderr
    assert loop.returncode == 2
    assert "paper-only" in loop.stderr


def test_no_live_options_execution_allowed(tmp_path: Path) -> None:
    root, log = _seed_repo(
        tmp_path,
        algo_script=_healthy_algo(),
        paper_log="OPTION_ORDER_SUBMITTED symbol=QQQ mode=live live option order\n",
    )

    proc = _run_health(root, tmp_path / "state", "--env", "paper", "--log-file", str(log))

    assert proc.returncode == 1
    assert "PAPER_OPTIONS_HEALTH status=unhealthy" in proc.stdout
    assert "PAPER_OPTIONS_HEALTH issue=live_options_execution_attempted" in proc.stdout


def test_prior_critical_options_health_report_does_not_poison_current_run(tmp_path: Path) -> None:
    root, log = _seed_repo(
        tmp_path,
        algo_script=_healthy_algo(),
        paper_log="\n".join(
            [
                "PAPER_OPTIONS_HEALTH issue=critical_options_errors",
                "fingerprint=paper-options:critical_options_errors",
                "PAPER_OPTIONS [PAPER] unstable: critical_options_errors",
                "Unstable quote NVDA260702C00275000",
                "OPTION_ROUTE_CHECK symbol=QQQ route=paper_options",
                "",
            ]
        ),
    )

    proc = _run_health(root, tmp_path / "state", "--env", "paper", "--log-file", str(log))

    assert proc.returncode == 0
    assert "PAPER_OPTIONS_HEALTH status=healthy" in proc.stdout
    assert "PAPER_OPTIONS_HEALTH issue=critical_options_errors" not in proc.stdout


def test_actual_critical_option_error_still_fails_health(tmp_path: Path) -> None:
    root, log = _seed_repo(
        tmp_path,
        algo_script=_healthy_algo(),
        paper_log="CRITICAL option chain loader failed for QQQ\n",
    )

    proc = _run_health(root, tmp_path / "state", "--env", "paper", "--log-file", str(log))

    assert proc.returncode == 1
    assert "PAPER_OPTIONS_HEALTH status=unhealthy" in proc.stdout
    assert "PAPER_OPTIONS_HEALTH issue=critical_options_errors" in proc.stdout
