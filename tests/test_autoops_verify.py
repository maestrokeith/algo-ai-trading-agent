from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import run_autoops


def _runtime_config_text(*, autostart: bool = True) -> str:
    return (
        "autoops:\n"
        f"  live_end_day_codex_autostart_enabled: {str(autostart).lower()}\n"
        "trading_control:\n"
        "  mode: shadow\n"
        "  strategy_states:\n"
        "    trend_long: SHADOW\n"
        "    momentum_breakout: SHADOW\n"
        "    dynamic_no_catalyst: SHADOW\n"
        "    news_only: DISABLED\n"
        "    options_live: DISABLED\n"
        "    options_paper: DISABLED\n"
        "  live_pilot:\n"
        "    enabled: false\n"
        "    allowed_strategies:\n"
        "      - trend_long\n"
        "    max_trades_per_day: 1\n"
        "    max_entry_submissions_per_day: 1\n"
        "    max_entry_fills_per_day: 1\n"
        "    max_open_positions: 1\n"
        "    max_notional_per_trade: 100\n"
        "    max_total_deployed_notional: 100\n"
        "    max_daily_loss_usd: 25\n"
        "    allow_short_selling: false\n"
        "    allow_add_to_existing: false\n"
        "    allow_replacements: false\n"
        "    allow_reallocation: false\n"
        "    allow_overnight: false\n"
        "    eod_flatten_required: true\n"
        "options:\n"
        "  enabled: false\n"
        "  mode: live_long_premium\n"
        "  live_pilot_enabled: false\n"
        "  live_pilot:\n"
        "    enabled: false\n"
    )


def _seed_root(root: Path) -> None:
    for rel in run_autoops.REQUIRED_AUTOOPS_PATHS:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test\n", encoding="utf-8")
        if rel.endswith(".sh") or rel.endswith(".py"):
            path.chmod(path.stat().st_mode | 0o111)
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "default.yaml").write_text(_runtime_config_text(), encoding="utf-8")
    (config / "users.yaml").write_text("users: []\n", encoding="utf-8")


def _write_autoops_config(root: Path, *, autostart: bool = True) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "default.yaml").write_text(_runtime_config_text(autostart=autostart), encoding="utf-8")
    (config / "users.yaml").write_text("users: []\n", encoding="utf-8")


def _patch_ready(
    monkeypatch,
    *,
    service_active: str = "active",
    timer: str = "active",
    endday_timer: str = "active",
    options: bool = False,
) -> None:
    monkeypatch.setattr(run_autoops, "_service_status", lambda **kwargs: {"service_active": service_active, "service_name": "algo.service"})
    monkeypatch.setattr(run_autoops, "_market_hours_now", lambda: False)
    monkeypatch.setattr(run_autoops, "_run_self_heal_dry_run", lambda root, environment: ("healthy", ""))
    monkeypatch.setattr(run_autoops, "_gh_authenticated", lambda: (True, "yes"))
    monkeypatch.setattr(run_autoops, "_required_github_labels_present", lambda: (True, []))
    monkeypatch.setattr(run_autoops, "_autoops_prs", lambda: [{"number": 1, "state": "OPEN", "labels": [{"name": "codex-validation-passed"}]}])
    monkeypatch.setattr(run_autoops, "_latest_autoops_issue", lambda: None)
    monkeypatch.setattr(run_autoops, "_latest_validation_status_for_environment", lambda *args, **kwargs: ("passed", "#1 live/open"))
    monkeypatch.setattr(
        run_autoops,
        "_systemd_unit_state",
        lambda unit: timer if unit == "intraday-health.timer" else "inactive",
    )
    monkeypatch.setattr(
        run_autoops,
        "_user_systemd_timer_ready",
        lambda unit, root=run_autoops.PROJECT_ROOT: (
            endday_timer == "active",
            f"enabled={'enabled' if endday_timer == 'active' else 'disabled'} active={endday_timer}",
        ),
    )
    monkeypatch.setattr(run_autoops, "_options_pilot_enabled", lambda root: options)
    monkeypatch.setattr(
        run_autoops,
        "_options_readiness",
        lambda root, environment: SimpleNamespace(
            config_enabled=True,
            mode="live_long_premium" if environment == "live" else "paper_only",
            live_pilot_enabled=environment == "live",
            long_premium_only=True,
            broker_supported=True,
            risk_limits_safe=True,
            route_active=True,
            final_status="ready",
            blocking_reasons=(),
        ),
    )
    monkeypatch.setattr(run_autoops, "_passwordless_sudo_status", lambda root: (True, "ok"))


def test_live_verify_passes_when_algo_active_or_expected_inactive(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    _patch_ready(monkeypatch, service_active="inactive", timer="active")
    monkeypatch.setattr(run_autoops, "_passwordless_sudo_status", lambda root: (True, "ok"))

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK service_active=inactive expected=expected_inactive_outside_market_hours" in out
    assert "AUTOOPS_VERIFY_CHECK passwordless_sudo=yes" in out
    assert "AUTOOPS_VERIFY_CHECK end_day_timer=yes" in out
    assert "AUTOOPS_VERIFY_CHECK end_day_codex_autostart=yes" in out
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in out


def test_live_verify_reports_passwordless_sudo_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    _patch_ready(monkeypatch, service_active="active", timer="active")
    monkeypatch.setattr(run_autoops, "_passwordless_sudo_status", lambda root: (False, "password required"))
    monkeypatch.setattr(run_autoops.getpass, "getuser", lambda: "algosphere")

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK passwordless_sudo=no" in out
    assert "sudo visudo -f /etc/sudoers.d/algo-autoops" in out
    assert "algosphere ALL=(root) NOPASSWD: /usr/bin/systemctl restart algo.service" in out
    assert "AUTOOPS_VERIFY_DETAIL sudo='password required'" in out


def test_live_verify_reports_graphql_bad_credentials(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    _patch_ready(monkeypatch, service_active="active", timer="active")
    monkeypatch.setattr(
        run_autoops,
        "_gh_authenticated",
        lambda: (False, "reason=bad_credentials_graphql check=graphql detail='HTTP 401: Bad credentials'"),
    )

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 1
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK github_authenticated=no" in out
    assert "AUTOOPS_VERIFY_DETAIL github_auth=reason=bad_credentials_graphql" in out
    assert "gh api graphql -f query='{ viewer { login } }'" in out
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=github_auth_unavailable" in out


def test_github_auth_check_succeeds_with_graphql_issue_and_pr_access(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return 0, "ok"

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    monkeypatch.setattr(run_autoops, "_run", fake_run)

    ok, detail = run_autoops._github_auth_check(tmp_path)

    assert ok is True
    assert "graphql=ok" in detail
    assert ["gh", "auth", "status"] in calls
    assert ["gh", "api", "graphql", "-f", "query={ viewer { login } }"] in calls
    assert ["gh", "issue", "list", "--repo", "YOUR_GITHUB_ORG/algo-ai-trading-agent", "--limit", "1"] in calls
    assert ["gh", "pr", "list", "--repo", "YOUR_GITHUB_ORG/algo-ai-trading-agent", "--limit", "1"] in calls


def test_github_auth_check_detects_graphql_401(monkeypatch, tmp_path: Path) -> None:
    def fake_run(args, **kwargs):
        argv = list(args)
        if argv[:3] == ["gh", "api", "graphql"]:
            return 1, "HTTP 401: Bad credentials (https://api.github.com/graphql)"
        return 0, "ok"

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    monkeypatch.setattr(run_autoops, "_run", fake_run)

    ok, detail = run_autoops._github_auth_check(tmp_path)

    assert ok is False
    assert "reason=bad_credentials_graphql" in detail
    assert "check=graphql" in detail


def test_live_verify_reports_end_day_timer_and_codex_autostart_disabled(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=False)
    _patch_ready(monkeypatch, service_active="active", timer="active", endday_timer="inactive")

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK end_day_timer=no" in out
    assert "AUTOOPS_VERIFY_CHECK end_day_codex_autostart=no" in out


def test_user_end_day_timer_detection_requires_enabled_and_active(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        argv = list(args)
        calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return 0, "enabled\n"
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return 0, "active\n"
        return 1, "unexpected"

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(run_autoops, "_run", fake_run)

    ready, detail = run_autoops._user_systemd_timer_ready("algosphere-live-endday-analysis.timer")

    assert ready is True
    assert detail == "enabled=enabled active=active"
    assert ["systemctl", "--user", "is-enabled", "algosphere-live-endday-analysis.timer"] in calls
    assert ["systemctl", "--user", "is-active", "algosphere-live-endday-analysis.timer"] in calls


def test_user_end_day_timer_detection_fails_when_inactive(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        argv = list(args)
        if argv[:3] == ["systemctl", "--user", "is-enabled"]:
            return 0, "enabled\n"
        if argv[:3] == ["systemctl", "--user", "is-active"]:
            return 3, "inactive\n"
        return 1, "unexpected"

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(run_autoops, "_run", fake_run)

    ready, detail = run_autoops._user_systemd_timer_ready("algosphere-live-endday-analysis.timer")

    assert ready is False
    assert detail == "enabled=enabled active=inactive"


def test_verify_required_labels_include_live_linux_processor(monkeypatch) -> None:
    payload = json.dumps([{"name": label} for label in run_autoops.REQUIRED_GITHUB_LABELS])

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: (0, payload))

    present, missing = run_autoops._required_github_labels_present()

    assert present is True
    assert missing == []


def test_verify_required_labels_report_missing_live_linux_processor(monkeypatch) -> None:
    labels = [label for label in run_autoops.REQUIRED_GITHUB_LABELS if label != "processor:live-linux"]
    payload = json.dumps([{"name": label} for label in labels])

    monkeypatch.setattr(run_autoops.shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(run_autoops, "_run", lambda args, **kwargs: (0, payload))

    present, missing = run_autoops._required_github_labels_present()

    assert present is False
    assert missing == ["processor:live-linux"]


def test_verify_fails_if_intraday_health_timer_disabled(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    _patch_ready(monkeypatch, service_active="active", timer="inactive")

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 1
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK intraday_health_timer=inactive" in out
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=intraday_health_timer_disabled" in out


def test_options_pilot_status_included_in_verify_output(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    health = tmp_path / "data" / "intraday_health" / "2026-06-29"
    health.mkdir(parents=True)
    (health / "live_intraday_health.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
    _patch_ready(monkeypatch, service_active="active", timer="active", options=True)

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK intraday_health_latest=healthy" in out
    assert "AUTOOPS_VERIFY_CHECK options_pilot=enabled" in out
    assert "AUTOOPS_VERIFY_CHECK options_enabled=yes" in out
    assert "AUTOOPS_VERIFY_CHECK options_mode=live_long_premium" in out
    assert "AUTOOPS_VERIFY_CHECK options_route_active=yes" in out


def test_live_verify_fails_when_enabled_options_gate_is_not_live_ready(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    _patch_ready(monkeypatch, service_active="active", timer="active", options=False)
    monkeypatch.setattr(run_autoops, "_effective_trading_mode", lambda root, environment: "live")
    monkeypatch.setattr(
        run_autoops,
        "_options_readiness",
        lambda root, environment: SimpleNamespace(
            config_enabled=True,
            mode="paper_only",
            live_pilot_enabled=False,
            long_premium_only=True,
            broker_supported=True,
            risk_limits_safe=True,
            route_active=False,
            final_status="inactive",
            blocking_reasons=("live_pilot_disabled", "mode_not_live"),
        ),
    )

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 1
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK options_live_pilot_enabled=no" in out
    assert "AUTOOPS_VERIFY_CHECK options_mode=paper_only" in out
    assert "AUTOOPS_VERIFY_CHECK options_route_active=no" in out
    assert "AUTOOPS_VERIFY_STATUS ready=false reason=options_live_pilot_disabled,mode_not_live" in out


def test_live_verify_disabled_options_gate_is_not_applicable(tmp_path: Path, monkeypatch, capsys) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    _patch_ready(monkeypatch, service_active="active", timer="active", options=False)
    monkeypatch.setattr(run_autoops, "_effective_trading_mode", lambda root, environment: "live")
    monkeypatch.setattr(run_autoops, "_runtime_profile", lambda root, environment: ("bounded_live_pilot", ()))
    monkeypatch.setattr(
        run_autoops,
        "_options_readiness",
        lambda root, environment: SimpleNamespace(
            config_enabled=False,
            mode="live_long_premium",
            live_pilot_enabled=False,
            long_premium_only=True,
            broker_supported=True,
            risk_limits_safe=True,
            route_active=False,
            final_status="inactive",
            blocking_reasons=("options_disabled", "live_pilot_disabled"),
        ),
    )

    rc = run_autoops._verify_readiness(tmp_path, environment="live")

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK options_enabled=no" in out
    assert "AUTOOPS_VERIFY_CHECK options_route_active=no" in out
    assert "AUTOOPS_VERIFY_DETAIL options_gate=not_applicable_for_bounded_live_pilot" in out
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in out


def test_paper_verify_macos_systemctl_unavailable_uses_file_log_fallback(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _seed_root(tmp_path)
    _write_autoops_config(tmp_path, autostart=True)
    monkeypatch.setattr(
        run_autoops,
        "_service_status",
        lambda **kwargs: {"service_active": "systemctl_unavailable", "service_name": "paper.service"},
    )
    monkeypatch.setattr(run_autoops, "_platform_name", lambda: "Darwin")
    monkeypatch.setattr(run_autoops, "_market_hours_now", lambda: True)
    monkeypatch.setattr(
        run_autoops,
        "_run_self_heal_dry_run",
        lambda root, environment: (
            "healthy",
            "SELF_HEAL_LOG_SOURCE source=file path=data/review/2026-06-29/paper_full.log\n"
            "SELF_HEAL status=healthy env=paper",
        ),
    )
    monkeypatch.setattr(run_autoops, "_gh_authenticated", lambda: (True, "yes"))
    monkeypatch.setattr(run_autoops, "_required_github_labels_present", lambda: (True, []))
    monkeypatch.setattr(run_autoops, "_autoops_prs", lambda: [{"number": 1, "state": "OPEN", "labels": [{"name": "codex-validation-passed"}]}])
    monkeypatch.setattr(run_autoops, "_latest_autoops_issue", lambda: None)
    monkeypatch.setattr(run_autoops, "_latest_validation_status_for_environment", lambda *args, **kwargs: ("passed", "#1 paper/open"))
    monkeypatch.setattr(run_autoops, "_systemd_unit_state", lambda unit: "systemctl_unavailable")
    monkeypatch.setattr(
        run_autoops,
        "_user_systemd_timer_ready",
        lambda unit, root=run_autoops.PROJECT_ROOT: (False, "systemctl_unavailable"),
    )
    monkeypatch.setattr(run_autoops, "_options_pilot_enabled", lambda root: False)

    rc = run_autoops._verify_readiness(tmp_path, environment="paper")

    assert rc == 0
    out = capsys.readouterr().out
    assert "AUTOOPS_VERIFY_CHECK service_active=systemctl_unavailable expected=paper_file_log_fallback_healthy" in out
    assert "AUTOOPS_VERIFY_CHECK intraday_health_timer=systemctl_unavailable" in out
    assert "AUTOOPS_VERIFY_CHECK paper_full_log_self_heal=readable" in out
    assert "AUTOOPS_VERIFY_STATUS ready=true reason=all_checks_passed" in out
