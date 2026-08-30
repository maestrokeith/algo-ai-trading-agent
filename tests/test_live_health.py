from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECK = PROJECT_ROOT / "scripts" / "check_live_health.sh"
LOOP = PROJECT_ROOT / "scripts" / "run_live_stabilization_loop.sh"
VERIFY = PROJECT_ROOT / "scripts" / "verify_codex_fix_health.sh"
DOCS = PROJECT_ROOT / "docs" / "OPERATIONS.md"


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _seed_root(tmp_path: Path, *, broker_ok: bool = True, open_orders_ok: bool = True) -> Path:
    root = tmp_path / "repo"
    (root / "data" / "premarket").mkdir(parents=True)
    for name in ("latest_event_feed.json", "latest_rankings.json", "latest_catalysts.json"):
        (root / "data" / "premarket" / name).write_text("{}", encoding="utf-8")
    _write_executable(
        root / "bin" / "algo",
        "#!/usr/bin/env bash\n"
        + ("echo 'summary equity=100 buying_power=100 open_orders=0'\nexit 0\n" if broker_ok else "echo broker failure >&2\nexit 7\n"),
    )
    _write_executable(
        root / "scripts" / "show_open_orders.py",
        "#!/usr/bin/env python3\n"
        + ("print('open_orders_context mode=live user=live_bot')\nprint('symbol\\tside\\tqty\\tstatus\\tsubmitted_at')\n" if open_orders_ok else "import sys\nprint('open_orders_unavailable: broker', file=sys.stderr)\nsys.exit(0)\n"),
    )
    return root


def _copy_live_scripts(root: Path) -> None:
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    for src in (CHECK, LOOP, VERIFY):
        dest = scripts / src.name
        shutil.copy2(src, dest)
        dest.chmod(src.stat().st_mode | stat.S_IXUSR)


def _fake_bin(
    tmp_path: Path,
    *,
    host: str = "algosphere-live-host",
    os_name: str = "Linux",
    service_state: str = "active",
    day_of_week: int = 1,
    logs: str = "INFO healthy",
    gh: str | None = None,
) -> Path:
    fake = tmp_path / "fake-bin"
    fake.mkdir(exist_ok=True)
    _write_executable(fake / "hostname", f"#!/usr/bin/env bash\necho {host!r}\n")
    _write_executable(fake / "uname", f"#!/usr/bin/env bash\necho {os_name!r}\n")
    _write_executable(
        fake / "systemctl",
        "#!/usr/bin/env bash\n"
        f"state={service_state!r}\n"
        "if [[ \"$1\" == \"is-active\" ]]; then\n"
        "  [[ \"$state\" == \"active\" ]] && echo active && exit 0\n"
        "  echo failed; exit 3\n"
        "fi\n"
        "echo \"$state\"\n",
    )
    _write_executable(fake / "journalctl", f"#!/usr/bin/env bash\necho {logs!r}\n")
    _write_executable(
        fake / "date",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"+%u\" ]]; then\n"
        f"  echo {day_of_week}\n"
        "  exit 0\n"
        "fi\n"
        "if [[ \"$1\" == \"+%Y%m%d\" ]]; then echo 20260615; exit 0; fi\n"
        "/bin/date \"$@\"\n",
    )
    if gh is not None:
        _write_executable(fake / "gh", gh)
    return fake


def _env(root: Path, fake_bin: Path, tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "ALGO_REPO_ROOT": str(root),
        "ALGO_LIVE_REQUIRED_STABLE_TICKS": "1",
        "ALGO_LIVE_STABLE_STATE_FILE": str(tmp_path / "stable_ticks"),
        "ALGO_LIVE_STABILIZATION_STATE_DIR": str(tmp_path / "live_stabilization_state"),
    }
    env.update(extra)
    return env


def _run(script: Path, root: Path, fake_bin: Path, tmp_path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(script), *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(root, fake_bin, tmp_path, **extra),
        check=False,
    )


def _make_stale(root: Path) -> None:
    old = time.time() - 48 * 60 * 60
    for path in (root / "data" / "premarket").iterdir():
        os.utime(path, (old, old))


def test_check_live_health_rejects_paper_env(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    fake = _fake_bin(tmp_path)
    proc = _run(CHECK, root, fake, tmp_path, "--env", "paper")
    assert proc.returncode == 2
    assert "rejects --env paper" in proc.stderr


def test_check_live_health_rejects_wrong_host(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    fake = _fake_bin(tmp_path, host="macbook", os_name="Darwin")
    proc = _run(CHECK, root, fake, tmp_path, "--env", "live")
    assert "LIVE_HEALTH status=unhealthy" in proc.stdout
    assert "LIVE_HEALTH issue=wrong_live_host" in proc.stdout


def test_check_live_health_healthy_live_status(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    fake = _fake_bin(tmp_path)
    proc = _run(CHECK, root, fake, tmp_path, "--env", "live")
    assert proc.returncode == 0
    assert "LIVE_HEALTH status=healthy" in proc.stdout
    assert "LIVE_HEALTH environment=live" in proc.stdout
    assert "LIVE_HEALTH stable_ticks=1" in proc.stdout


def test_check_live_health_unhealthy_service_down(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    fake = _fake_bin(tmp_path, service_state="failed")
    proc = _run(CHECK, root, fake, tmp_path, "--env", "live")
    assert "LIVE_HEALTH status=unhealthy" in proc.stdout
    assert "LIVE_HEALTH issue=service_down" in proc.stdout


def test_check_live_health_weekend_stale_premarket_suppressed(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    _make_stale(root)
    fake = _fake_bin(tmp_path, day_of_week=7)
    proc = _run(CHECK, root, fake, tmp_path, "--env", "live", ALGO_LIVE_PREMARKET_MAX_AGE_MINUTES="1")
    assert "LIVE_HEALTH status=healthy" in proc.stdout
    assert "LIVE_HEALTH premarket=stale_suppressed:weekend_market_closed" in proc.stdout


def test_check_live_health_weekday_stale_premarket_actionable(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    _make_stale(root)
    fake = _fake_bin(tmp_path, day_of_week=1)
    proc = _run(CHECK, root, fake, tmp_path, "--env", "live", ALGO_LIVE_PREMARKET_MAX_AGE_MINUTES="1")
    assert "LIVE_HEALTH status=unhealthy" in proc.stdout
    assert "LIVE_HEALTH issue=premarket_artifact_stale" in proc.stdout


def test_check_live_health_broker_failure_unhealthy(tmp_path: Path) -> None:
    root = _seed_root(tmp_path, broker_ok=False)
    fake = _fake_bin(tmp_path)
    proc = _run(CHECK, root, fake, tmp_path, "--env", "live")
    assert "LIVE_HEALTH status=unhealthy" in proc.stdout
    assert "LIVE_HEALTH issue=broker_account_unreadable" in proc.stdout


def test_check_live_health_live_options_unapproved_unhealthy(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    fake = _fake_bin(tmp_path, logs="OPTION_ORDER_SUBMITTED live options route")
    proc = _run(CHECK, root, fake, tmp_path, "--env", "live")
    assert "LIVE_HEALTH status=unhealthy" in proc.stdout
    assert "LIVE_HEALTH issue=live_options_unapproved" in proc.stdout


def test_live_stabilization_loop_creates_issue(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    _copy_live_scripts(root)
    calls = tmp_path / "gh_calls.log"
    fake = _fake_bin(
        tmp_path,
        service_state="failed",
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )
    proc = _run(root / "scripts" / "run_live_stabilization_loop.sh", root, fake, tmp_path)
    assert proc.returncode == 0
    assert "LIVE_STABILIZATION status=repair_issue_created" in proc.stdout
    gh_calls = calls.read_text(encoding="utf-8")
    assert "--title LIVE_STABILIZATION [LIVE] unstable: service_down" in gh_calls
    assert "--label environment:live" in gh_calls
    assert "--label processor:live-linux" in gh_calls
    assert "--label live-stabilization" in gh_calls
    assert "--label needs-human-review" in gh_calls


def test_live_stabilization_duplicate_issue_detection(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    _copy_live_scripts(root)
    calls = tmp_path / "gh_calls.log"
    fake = _fake_bin(
        tmp_path,
        service_state="failed",
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[{\"body\":\"live-stabilization:live:service_down\"}]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 42; fi\n"
        ),
    )
    proc = _run(root / "scripts" / "run_live_stabilization_loop.sh", root, fake, tmp_path)
    assert proc.returncode == 0
    assert "Existing live stabilization issue found" in proc.stdout
    assert "issue create" not in calls.read_text(encoding="utf-8")


def test_live_stabilization_max_attempts_stops_loop(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    _copy_live_scripts(root)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "live_20260615.count").write_text("1", encoding="utf-8")
    fake = _fake_bin(tmp_path, service_state="failed")
    proc = _run(
        root / "scripts" / "run_live_stabilization_loop.sh",
        root,
        fake,
        tmp_path,
        ALGO_LIVE_STABILIZATION_STATE_DIR=str(state_dir),
        ALGO_LIVE_MAX_REPAIR_ATTEMPTS_PER_DAY="1",
    )
    assert proc.returncode == 0
    assert "LIVE_STABILIZATION status=needs_human_review" in proc.stdout


def test_live_scripts_have_no_auto_restart_deploy_or_order_behavior() -> None:
    text = (CHECK.read_text(encoding="utf-8") + LOOP.read_text(encoding="utf-8")).lower()
    forbidden = [
        "systemctl restart",
        "gh pr merge",
        "git pull",
        "submit_order(",
        "cancel_order(",
        "close_position(",
        "enable live options",
    ]
    for needle in forbidden:
        assert needle not in text
    assert "needs-human-review" in LOOP.read_text(encoding="utf-8")


def test_postfix_live_unhealthy_creates_followup(tmp_path: Path) -> None:
    root = _seed_root(tmp_path)
    _copy_live_scripts(root)
    calls = tmp_path / "gh_calls.log"
    fake = _fake_bin(
        tmp_path,
        service_state="failed",
        gh=(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {calls}\n"
            "if [[ \"$1 $2\" == \"issue list\" ]]; then printf '[]\\n'; exit 0; fi\n"
            "if [[ \"$1 $2\" == \"issue create\" ]]; then exit 0; fi\n"
        ),
    )
    proc = _run(root / "scripts" / "verify_codex_fix_health.sh", root, fake, tmp_path, "--env", "live", "--issue", "120", "--pr", "45")
    assert proc.returncode == 0
    assert "POST_FIX_VERIFICATION status=unhealthy env=live" in proc.stdout
    assert "--title POSTFIX [LIVE] repair still unhealthy after PR #45" in calls.read_text(encoding="utf-8")


def test_operations_docs_cover_live_stabilization() -> None:
    text = DOCS.read_text(encoding="utf-8")
    assert "Live Stabilization Loop" in text
    assert "scripts/run_live_stabilization_loop.sh --dry-run" in text
    assert "Live fixes must remain infrastructure/diagnostic/safety-only" in text
